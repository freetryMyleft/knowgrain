"""Journaled filesystem application for explicitly reviewed Wiki pages."""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any
from uuid import UUID

from knowgrain.vault import VaultPathError
from knowgrain.wiki_files import (
    MAX_PAGE_BYTES,
    WikiConflictError,
    WikiFile,
    WikiFileStore,
    WikiNotFoundError,
    WikiValidationError,
    _fsync_directory,
    _hash_bytes,
    parse_wiki,
)


# At most two 2 MiB pages are embedded as base64, plus bounded JSON metadata.
MAX_REVIEW_INTENT_BYTES = 6 * 1024 * 1024
_INTENT_SCHEMA = 1
_INTENT_KEYS = {
    "schema_version",
    "operation_id",
    "page_id",
    "old_path",
    "old_sha256",
    "old_bytes_b64",
    "new_path",
    "new_sha256",
    "new_bytes_b64",
}
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReviewFileStore(WikiFileStore):
    """Commit an explicitly reviewed projection with durable retry state.

    The caller is responsible for authorization, provenance, and preparing the
    corresponding database operation. This class only changes managed Wiki
    files and its private recovery journal.
    """

    def __init__(self, files: WikiFileStore) -> None:
        # Keep the same dependency boundary as GenerationFileStore: services
        # hand this operation the already configured WikiFileStore.
        if not isinstance(files, WikiFileStore):
            raise TypeError("ReviewFileStore requires a WikiFileStore")
        super().__init__(files.vault)

    def commit(
        self,
        operation_id: UUID,
        page_id: UUID,
        expected_sha256: str,
        markdown: str,
    ) -> WikiFile:
        operation_id = self._coerce_uuid(operation_id, "operation_id")
        page_id = self._coerce_uuid(page_id, "page_id")
        if not isinstance(expected_sha256, str) or not _HASH_PATTERN.fullmatch(expected_sha256):
            raise WikiValidationError("expected_sha256 must be a lowercase SHA-256 hash")

        requested = self._validate_new_markdown(page_id, markdown)
        intent_path = self._intent_path(operation_id)
        raw_intent = self._read_internal_optional(intent_path)
        if raw_intent is None:
            intent = self._prepare_intent(
                operation_id, page_id, expected_sha256, requested
            )
            raw_intent = self._encode_intent(intent)
            self._publish_intent(intent_path, raw_intent)
        else:
            # The previous process may have published the intent but failed
            # while syncing one of its directories. Re-sync the full existing
            # chain and the journal bytes before any Wiki file can change.
            self._sync_intent_durability(intent_path)
            raw_intent = self._read_internal_optional(intent_path)
            if raw_intent is None:
                raise self._conflict(
                    "Review intent disappeared during recovery", "review_intent_invalid"
                )

        intent = self._decode_intent(raw_intent)
        self._validate_intent_request(
            intent,
            operation_id=operation_id,
            page_id=page_id,
            expected_sha256=expected_sha256,
            requested_bytes=requested,
        )
        old_bytes = intent["old_bytes"]
        new_bytes = intent["new_bytes"]
        old_path = intent["old_path"]
        new_path = intent["new_path"]

        assert isinstance(old_path, str)
        assert isinstance(new_path, str)
        state = self._state(intent)
        allowed_duplicates = (
            {old_path, new_path} if state == "move_both" else set()
        )
        self._validate_relevant_scan(page_id, allowed_duplicates)

        # Keep the original bytes recoverable even on retries after a completed
        # write. A changed recovery snapshot is a conflict and stops the commit.
        try:
            self._publish_recovery(page_id, intent["old_sha256"], old_bytes)
            if state == "same_path_old":
                self._replace_same_path(intent)
            elif state == "move_old_only":
                self._publish_move_destination(intent)
                self._remove_old_after_move(intent)
            elif state == "move_both":
                self._remove_old_after_move(intent)
            elif state in {"same_path_new", "move_new_only"}:
                pass
            else:  # pragma: no cover - _state is exhaustive by construction.
                raise AssertionError(f"unknown review file state: {state}")
        except WikiConflictError:
            raise
        except (OSError, VaultPathError, WikiValidationError) as exc:
            raise WikiConflictError(
                "Reviewed Wiki files could not be safely committed; retry the same operation",
                current=None,
                code="review_write_failed",
                diff="",
            ) from exc

        return parse_wiki(intent["new_markdown"], new_path)

    def validate_retry_intent(
        self,
        operation_id: UUID,
        page_id: UUID,
        expected_sha256: str,
        markdown: str,
    ) -> bool:
        """Prove a duplicate Wiki identity belongs to this durable retry.

        This method is deliberately read-only with respect to Wiki files. It
        may sync the private intent journal to disk, but it never creates an
        intent or repairs the Wiki projection. Callers can use ``True`` as
        authorization to reconcile only the exact partial state described by
        this operation's immutable intent.
        """
        operation_id = self._coerce_uuid(operation_id, "operation_id")
        page_id = self._coerce_uuid(page_id, "page_id")
        if not isinstance(expected_sha256, str) or not _HASH_PATTERN.fullmatch(expected_sha256):
            raise WikiValidationError("expected_sha256 must be a lowercase SHA-256 hash")
        requested = self._validate_new_markdown(page_id, markdown)
        intent_path = self._intent_path(operation_id)
        raw_intent = self._read_internal_optional(intent_path)
        if raw_intent is None:
            return False

        # A prior attempt may have linked the intent and then failed during a
        # directory fsync. Do not treat its bytes as retry authority until the
        # full parent chain and file have been strictly synced on this attempt.
        self._sync_intent_durability(intent_path)
        raw_intent = self._read_internal_optional(intent_path)
        if raw_intent is None:
            raise self._conflict(
                "Review intent disappeared during retry validation", "review_intent_invalid"
            )

        intent = self._decode_intent(raw_intent)
        self._validate_intent_request(
            intent,
            operation_id=operation_id,
            page_id=page_id,
            expected_sha256=expected_sha256,
            requested_bytes=requested,
        )
        state = self._state(intent)
        old_path = intent["old_path"]
        new_path = intent["new_path"]
        assert isinstance(old_path, str) and isinstance(new_path, str)
        allowed_duplicates = {old_path, new_path} if state == "move_both" else set()
        self._validate_relevant_scan(page_id, allowed_duplicates)
        return True

    @staticmethod
    def _coerce_uuid(value: UUID, name: str) -> UUID:
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WikiValidationError(f"{name} must be a UUID") from exc

    @staticmethod
    def _validate_new_markdown(page_id: UUID, markdown: str) -> bytes:
        # The final managed path is validated once it has been selected from
        # the existing page identity and the immutable intent.
        candidate = parse_wiki(markdown, f"Wiki/Pages/{page_id}.md")
        if candidate.page_id != page_id:
            raise WikiValidationError("reviewed Markdown page identity does not match page_id")
        if candidate.status != "reviewed":
            raise WikiValidationError("reviewed Markdown must have kg_status: reviewed")
        return markdown.encode("utf-8", errors="strict")

    def _prepare_intent(
        self,
        operation_id: UUID,
        page_id: UUID,
        expected_sha256: str,
        new_bytes: bytes,
    ) -> dict[str, Any]:
        scan = self.scan()
        self._ensure_scan_is_safe(scan)
        self._reject_target_duplicates(scan, page_id)
        page = next((item for item in scan.pages if item.page_id == page_id), None)
        if page is None:
            # A canonical page path whose contents became malformed or changed
            # identity is not equivalent to an absent target. Preserve it and
            # report a conflict instead of silently treating it as missing.
            for relative in (
                f"Wiki/Drafts/{page_id}.md",
                f"Wiki/Pages/{page_id}.md",
            ):
                try:
                    existing = self._read_managed_optional(relative)
                except (OSError, VaultPathError, WikiValidationError) as exc:
                    raise self._conflict(
                        "Wiki page changed or became unsafe before review could be committed",
                        "page_changed",
                    ) from exc
                if existing is not None:
                    raise self._conflict(
                        "Wiki page identity or content changed before review could be committed",
                        "page_changed",
                    )
            raise WikiNotFoundError(f"Wiki page not found: {page_id}")

        old_path = page.vault_path
        try:
            old_bytes = self._read_regular(old_path)
        except (OSError, VaultPathError, WikiValidationError) as exc:
            raise self._conflict(
                "Wiki page changed before review could be committed", "page_changed"
            ) from exc
        old_hash = _hash_bytes(old_bytes)
        if old_hash != expected_sha256:
            raise self._conflict("Wiki page content changed since it was read", "stale_content")
        old_page = self._parse_bytes(old_bytes, old_path)
        if old_page.page_id != page_id:
            raise self._conflict(
                "Wiki page identity changed before review", "identity_changed"
            )

        if old_path.startswith("Wiki/Pages/"):
            new_path = old_path
        else:
            new_path = f"Wiki/Pages/{page_id}.md"
        new_page = self._parse_bytes(new_bytes, new_path)
        if new_page.page_id != page_id or new_page.status != "reviewed":
            raise WikiValidationError("reviewed Markdown identity or status is invalid")

        if new_path != old_path:
            destination_bytes = self._read_managed_optional(new_path)
            if destination_bytes is not None:
                raise self._conflict(
                    "A different Wiki file already occupies the reviewed page path",
                    "destination_exists",
                )

        return {
            "schema_version": _INTENT_SCHEMA,
            "operation_id": str(operation_id),
            "page_id": str(page_id),
            "old_path": old_path,
            "old_sha256": old_hash,
            "old_bytes": old_bytes,
            "new_path": new_path,
            "new_sha256": _hash_bytes(new_bytes),
            "new_bytes": new_bytes,
            "new_markdown": new_bytes.decode("utf-8", errors="strict"),
        }

    def _validate_relevant_scan(self, page_id: UUID, allowed_duplicate_paths: set[str]) -> None:
        scan = self.scan()
        self._ensure_scan_is_safe(scan)
        self._reject_target_duplicates(scan, page_id, allowed_duplicate_paths)

    @staticmethod
    def _ensure_scan_is_safe(scan: Any) -> None:
        if not scan.complete:
            raise WikiConflictError(
                "Wiki scan is incomplete; page identities cannot be resolved safely",
                current=None,
                code="scan_incomplete",
                diff="",
            )
        if any(issue.code == "unsafe_path" for issue in scan.issues):
            raise WikiConflictError(
                "A managed Wiki path is unsafe; review commit stopped",
                current=None,
                code="unsafe_path",
                diff="",
            )

    @staticmethod
    def _reject_target_duplicates(
        scan: Any,
        page_id: UUID,
        allowed_duplicate_paths: set[str] | None = None,
    ) -> None:
        duplicates = {
            issue.vault_path
            for issue in scan.issues
            if issue.code == "duplicate_id" and issue.detail.startswith(f"page id {page_id} ")
        }
        allowed = allowed_duplicate_paths or set()
        if duplicates != allowed:
            raise WikiConflictError(
                "Wiki page identity is duplicated or ambiguous",
                current=None,
                code="duplicate_id",
                diff="",
            )

    def _state(self, intent: dict[str, Any]) -> str:
        old_path = intent["old_path"]
        new_path = intent["new_path"]
        old_bytes = intent["old_bytes"]
        new_bytes = intent["new_bytes"]
        assert isinstance(old_path, str) and isinstance(new_path, str)

        old_current = self._read_managed_optional(old_path)
        if old_path == new_path:
            if old_current == old_bytes:
                return "same_path_old"
            if old_current == new_bytes:
                return "same_path_new"
            raise self._conflict(
                "Wiki page changed after the review intent was prepared", "stale_content"
            )

        new_current = self._read_managed_optional(new_path)
        if old_current == old_bytes and new_current is None:
            return "move_old_only"
        if old_current == old_bytes and new_current == new_bytes:
            return "move_both"
        if old_current is None and new_current == new_bytes:
            return "move_new_only"
        raise self._conflict(
            "Wiki files no longer match the prepared review operation",
            "review_state_changed",
        )

    def _replace_same_path(self, intent: dict[str, Any]) -> None:
        relative = intent["old_path"]
        new_bytes = intent["new_bytes"]
        assert isinstance(relative, str)
        destination = self._safe_path(relative)
        temporary_path = self._write_sibling(destination, new_bytes)
        try:
            current = self._read_regular(relative)
            self._verify_old_bytes(current, intent)
            self._safe_path(relative)
            os.replace(temporary_path, destination)
            _fsync_directory(destination.parent)
        except Exception:
            self._unlink_temporary(temporary_path)
            raise

    def _publish_move_destination(self, intent: dict[str, Any]) -> None:
        new_path = intent["new_path"]
        new_bytes = intent["new_bytes"]
        assert isinstance(new_path, str)
        destination = self._safe_path(new_path)
        try:
            self._publish_exclusive(destination, new_bytes)
        except WikiConflictError as exc:
            # A concurrent retry may have published the exact intended bytes.
            # It is safe to continue only after proving that exact state.
            try:
                current = self._read_regular(new_path)
            except (OSError, VaultPathError, WikiValidationError):
                raise exc
            if current != new_bytes:
                raise self._conflict(
                    "A different file appeared at the reviewed page path",
                    "destination_changed",
                ) from exc

    def _remove_old_after_move(self, intent: dict[str, Any]) -> None:
        old_path = intent["old_path"]
        assert isinstance(old_path, str)
        old_path_obj = self._safe_path(old_path)
        current = self._read_regular(old_path)
        self._verify_old_bytes(current, intent)
        self._safe_path(old_path)
        os.unlink(old_path_obj)
        _fsync_directory(old_path_obj.parent)

    def _verify_old_bytes(self, content: bytes, intent: dict[str, Any]) -> None:
        if content != intent["old_bytes"]:
            raise self._conflict(
                "Original Wiki bytes changed during review commit; both versions were preserved",
                "stale_content",
            )
        old_path = intent["old_path"]
        assert isinstance(old_path, str)
        old_page = self._parse_bytes(content, old_path)
        if old_page.page_id != UUID(intent["page_id"]):
            raise self._conflict(
                "Original Wiki identity changed during review commit", "identity_changed"
            )

    @staticmethod
    def _parse_bytes(content: bytes, relative_path: str) -> WikiFile:
        try:
            markdown = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WikiValidationError("Wiki page is not valid UTF-8") from exc
        return parse_wiki(markdown, relative_path)

    def _validate_intent_request(
        self,
        intent: dict[str, Any],
        *,
        operation_id: UUID,
        page_id: UUID,
        expected_sha256: str,
        requested_bytes: bytes,
    ) -> None:
        if (
            intent["operation_id"] != str(operation_id)
            or intent["page_id"] != str(page_id)
            or intent["old_sha256"] != expected_sha256
            or intent["new_bytes"] != requested_bytes
        ):
            raise self._conflict(
                "Review operation ID already has a different immutable intent",
                "review_intent_mismatch",
            )
        old_path = intent["old_path"]
        new_path = intent["new_path"]
        if not isinstance(old_path, str) or not isinstance(new_path, str):
            raise self._conflict("Review intent paths are invalid", "review_intent_invalid")
        try:
            checked_old = self._safe_path(old_path)
            checked_new = self._safe_path(new_path)
        except (WikiValidationError, VaultPathError) as exc:
            raise self._conflict(
                "Review intent contains an unsafe Wiki path", "review_intent_invalid"
            ) from exc
        if (
            checked_old.relative_to(self.vault.root).as_posix() != old_path
            or checked_new.relative_to(self.vault.root).as_posix() != new_path
        ):
            raise self._conflict(
                "Review intent paths are not canonical", "review_intent_invalid"
            )
        expected_new_path = (
            old_path if old_path.startswith("Wiki/Pages/") else f"Wiki/Pages/{page_id}.md"
        )
        if new_path != expected_new_path:
            raise self._conflict(
                "Review intent destination does not match the page identity",
                "review_intent_invalid",
            )
        try:
            old_page = self._parse_bytes(intent["old_bytes"], old_path)
            new_page = self._parse_bytes(intent["new_bytes"], new_path)
        except WikiValidationError as exc:
            raise self._conflict(
                "Review intent contains invalid Wiki Markdown", "review_intent_invalid"
            ) from exc
        if (
            old_page.page_id != page_id
            or new_page.page_id != page_id
            or new_page.status != "reviewed"
        ):
            raise self._conflict(
                "Review intent identity or status is invalid", "review_intent_invalid"
            )

    def _intent_path(self, operation_id: UUID) -> str:
        return f".knowgrain/review-operations/{operation_id}/intent.json"

    @staticmethod
    def _encode_intent(intent: dict[str, Any]) -> bytes:
        document = {
            "schema_version": intent["schema_version"],
            "operation_id": intent["operation_id"],
            "page_id": intent["page_id"],
            "old_path": intent["old_path"],
            "old_sha256": intent["old_sha256"],
            "old_bytes_b64": base64.b64encode(intent["old_bytes"]).decode("ascii"),
            "new_path": intent["new_path"],
            "new_sha256": intent["new_sha256"],
            "new_bytes_b64": base64.b64encode(intent["new_bytes"]).decode("ascii"),
        }
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_REVIEW_INTENT_BYTES:
            raise WikiValidationError("Review intent exceeds the supported size limit")
        return encoded

    def _decode_intent(self, encoded: bytes) -> dict[str, Any]:
        if len(encoded) > MAX_REVIEW_INTENT_BYTES:
            raise self._conflict(
                "Review intent exceeds the supported size limit", "review_intent_invalid"
            )
        try:
            document = json.loads(
                encoded.decode("utf-8", errors="strict"),
                object_pairs_hook=self._unique_pairs,
            )
            if not isinstance(document, dict) or set(document) != _INTENT_KEYS:
                raise ValueError("unexpected review intent fields")
            canonical = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if canonical != encoded:
                raise ValueError("review intent is not canonical")
            if document["schema_version"] != _INTENT_SCHEMA:
                raise ValueError("unsupported review intent version")
            operation_id = UUID(document["operation_id"])
            page_id = UUID(document["page_id"])
            if str(operation_id) != document["operation_id"] or str(page_id) != document["page_id"]:
                raise ValueError("non-canonical review identity")
            old_hash = document["old_sha256"]
            new_hash = document["new_sha256"]
            if not isinstance(old_hash, str) or not _HASH_PATTERN.fullmatch(old_hash):
                raise ValueError("invalid original hash")
            if not isinstance(new_hash, str) or not _HASH_PATTERN.fullmatch(new_hash):
                raise ValueError("invalid reviewed hash")
            old_bytes = base64.b64decode(document["old_bytes_b64"], validate=True)
            new_bytes = base64.b64decode(document["new_bytes_b64"], validate=True)
            if len(old_bytes) > MAX_PAGE_BYTES or len(new_bytes) > MAX_PAGE_BYTES:
                raise ValueError("review page exceeds the supported size limit")
            if _hash_bytes(old_bytes) != old_hash or _hash_bytes(new_bytes) != new_hash:
                raise ValueError("review intent byte hash mismatch")
            if not isinstance(document["old_path"], str) or not isinstance(
                document["new_path"], str
            ):
                raise ValueError("invalid review intent paths")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            AttributeError,
            binascii.Error,
        ) as exc:
            raise self._conflict(
                "Review intent is malformed or has changed", "review_intent_invalid"
            ) from exc

        return {
            "schema_version": _INTENT_SCHEMA,
            "operation_id": str(operation_id),
            "page_id": str(page_id),
            "old_path": document["old_path"],
            "old_sha256": old_hash,
            "old_bytes": old_bytes,
            "new_path": document["new_path"],
            "new_sha256": new_hash,
            "new_bytes": new_bytes,
            "new_markdown": self._decode_markdown(new_bytes),
        }

    @staticmethod
    def _decode_markdown(content: bytes) -> str:
        try:
            return content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WikiValidationError("review intent Markdown is not valid UTF-8") from exc

    @staticmethod
    def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def _publish_intent(self, relative_path: str, content: bytes) -> None:
        destination = self._safe_internal_path(relative_path)
        self._ensure_internal_parent(relative_path)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".knowgrain-review-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            self._safe_internal_path(relative_path)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                existing = self._read_internal_optional(relative_path)
                if existing != content:
                    raise self._conflict(
                        "Review operation ID already has a different immutable intent",
                        "review_intent_mismatch",
                    )
            # This strict path is intentionally separate from WikiFileStore's
            # best-effort directory sync: without a durable journal chain the
            # caller must not mutate the authoritative Wiki file.
            self._sync_intent_durability(relative_path)
        finally:
            if temporary_path is not None:
                self._unlink_temporary(temporary_path)

    def _read_internal_optional(self, relative_path: str) -> bytes | None:
        path = self._safe_internal_path(relative_path)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise VaultPathError("Review journal must be a regular file")
            if metadata.st_size > MAX_REVIEW_INTENT_BYTES:
                raise self._conflict(
                    "Review intent exceeds the supported size limit", "review_intent_invalid"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                content = file.read(MAX_REVIEW_INTENT_BYTES + 1)
            if len(content) > MAX_REVIEW_INTENT_BYTES:
                raise self._conflict(
                    "Review intent exceeds the supported size limit", "review_intent_invalid"
                )
            return content
        finally:
            os.close(descriptor)

    def _ensure_internal_parent(self, relative_path: str) -> None:
        parent = PurePosixPath(relative_path).parent
        built: list[str] = []
        for component in parent.parts:
            built.append(component)
            relative = "/".join(built)
            directory = self._safe_internal_path(relative)
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            else:
                # Persist each new directory entry in its parent before
                # creating the next level.
                self._strict_fsync_directory(directory.parent)
            self._safe_internal_path(relative)
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise VaultPathError("Review journal parent must be a directory")
        # Existing directories can be left unsynced by an earlier interrupted
        # attempt, so always sync the whole chain, even when mkdir did nothing.
        self._sync_internal_directory_chain(relative_path)

    def _sync_intent_durability(self, relative_path: str) -> None:
        self._sync_internal_directory_chain(relative_path)
        intent_path = self._safe_internal_path(relative_path)
        self._strict_fsync_regular_file(intent_path)
        # Persist the journal's directory entry after syncing its contents.
        self._strict_fsync_directory(intent_path.parent)

    def _sync_internal_directory_chain(self, relative_path: str) -> None:
        self._safe_internal_path(relative_path)
        self._strict_fsync_directory(self.vault.root)
        current = self.vault.root
        for component in PurePosixPath(relative_path).parent.parts:
            current = current / component
            self._safe_internal_path(current.relative_to(self.vault.root).as_posix())
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise VaultPathError("Review journal parent must be a directory")
            self._strict_fsync_directory(current)

    @staticmethod
    def _strict_fsync_directory(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise VaultPathError("Review journal path is not a directory")
            # Directory fsync is required for journal durability. Propagate
            # platform errors so commit stops before modifying Wiki content.
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _strict_fsync_regular_file(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VaultPathError("Review journal must be a regular file")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _safe_internal_path(self, relative_path: str) -> Path:
        if (
            not isinstance(relative_path, str)
            or relative_path
            not in {".knowgrain", ".knowgrain/review-operations"}
            and not relative_path.startswith(".knowgrain/review-operations/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative_path).parts)
        ):
            raise VaultPathError("Review journal path is invalid")
        return self.vault.resolve(relative_path)

    def _read_managed_optional(self, relative_path: str) -> bytes | None:
        self._safe_path(relative_path)
        try:
            return self._read_regular(relative_path)
        except FileNotFoundError:
            return None

    @staticmethod
    def _conflict(message: str, code: str) -> WikiConflictError:
        return WikiConflictError(message, current=None, code=code, diff="")
