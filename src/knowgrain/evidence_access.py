"""Safe publication and byte-backed reads for retained M4 evidence."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from knowgrain.generation_contract import DraftValidationError, render_evidence
from knowgrain.m3_types import Evidence
from knowgrain.vault import VaultPathError, VaultStore


_MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
_MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


class EvidenceFileError(RuntimeError):
    """A retained evidence file is missing, changed, or unsafe to access."""

    def __init__(self, code: str) -> None:
        if code not in {"missing", "conflict", "unavailable"}:
            raise ValueError("invalid evidence file error code")
        self.code = code
        messages = {
            "missing": "Evidence file is missing.",
            "conflict": "Evidence file differs from its retained revision.",
            "unavailable": "Evidence file cannot be accessed safely.",
        }
        super().__init__(messages[code])


class EvidenceAccess:
    """Read exact source revisions and publish canonical evidence Markdown."""

    def __init__(self, vault: VaultStore) -> None:
        self.vault = vault

    def publish(self, evidence: Sequence[Evidence]) -> None:
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise EvidenceFileError("unavailable")
        items = list(evidence)
        if len(items) > 24 or len({item.evidence_id for item in items if isinstance(item, Evidence)}) != len(items):
            raise EvidenceFileError("unavailable")
        for item in items:
            relative, content = self._canonical_markdown(item)
            if len(content) > _MAX_MARKDOWN_BYTES:
                raise EvidenceFileError("unavailable")
            self._publish_exclusive(relative, content)

    def original(self, evidence: Evidence) -> tuple[bytes, str]:
        self._canonical_markdown(evidence)  # Validate the retained evidence identity and fields.
        relative = evidence.vault_path
        captured, digest = self._capture(relative, _MAX_ORIGINAL_BYTES)
        if digest != evidence.source_sha256:
            raise EvidenceFileError("conflict")
        return captured, evidence.filename

    def markdown(self, evidence: Evidence) -> bytes:
        relative, expected = self._canonical_markdown(evidence)
        captured, digest = self._capture(relative, _MAX_MARKDOWN_BYTES)
        if digest != hashlib.sha256(expected).hexdigest() or captured != expected:
            raise EvidenceFileError("conflict")
        return captured

    def _canonical_markdown(self, evidence: Evidence) -> tuple[str, bytes]:
        if not isinstance(evidence, Evidence):
            raise EvidenceFileError("unavailable")
        try:
            markdown = render_evidence(evidence)
            evidence_id = UUID(str(evidence.evidence_id))
            relative = f"Sources/Evidence/{evidence_id}.md"
            self.vault.resolve(relative)
            return relative, markdown.encode("utf-8", errors="strict")
        except (DraftValidationError, ValueError, TypeError, UnicodeError, VaultPathError):
            raise EvidenceFileError("unavailable") from None

    def _capture(self, relative: str, maximum_bytes: int) -> tuple[bytes, str]:
        try:
            safe_path = self.vault.resolve(relative)
            path_relative = safe_path.relative_to(self.vault.root)
            parent_fd = self._open_directory_chain(path_relative.parts[:-1])
        except FileNotFoundError:
            raise EvidenceFileError("missing") from None
        except (OSError, ValueError, VaultPathError):
            raise EvidenceFileError("unavailable") from None

        filename = path_relative.parts[-1]
        try:
            descriptor = os.open(filename, _READ_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            os.close(parent_fd)
            raise EvidenceFileError("missing") from None
        except OSError as exc:
            os.close(parent_fd)
            if exc.errno == errno.ELOOP:
                raise EvidenceFileError("unavailable") from None
            raise EvidenceFileError("unavailable") from None
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
                raise EvidenceFileError("unavailable")
            try:
                path_before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise EvidenceFileError("missing") from None
            if not self._same_file_identity(before, path_before):
                raise EvidenceFileError("conflict")

            data = bytearray()
            digest = hashlib.sha256()
            while True:
                block = os.read(descriptor, min(_READ_CHUNK_BYTES, maximum_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                digest.update(block)
                if len(data) > maximum_bytes:
                    raise EvidenceFileError("unavailable")

            after = os.fstat(descriptor)
            try:
                path_after = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise EvidenceFileError("conflict") from None
            if not self._same_stat(before, after) or not self._same_file_identity(after, path_after):
                raise EvidenceFileError("conflict")
            return bytes(data), digest.hexdigest()
        finally:
            os.close(descriptor)
            os.close(parent_fd)

    def _publish_exclusive(self, relative: str, content: bytes) -> None:
        try:
            safe_path = self.vault.resolve(relative)
            path_relative = safe_path.relative_to(self.vault.root)
            parent_fd = self._open_directory_chain(path_relative.parts[:-1])
        except FileNotFoundError:
            raise EvidenceFileError("unavailable") from None
        except (OSError, ValueError, VaultPathError):
            raise EvidenceFileError("unavailable") from None

        destination = path_relative.parts[-1]
        temporary = f".knowgrain-evidence-{secrets.token_hex(12)}.tmp"
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=parent_fd)
            except OSError:
                raise EvidenceFileError("unavailable") from None
            with os.fdopen(descriptor, "wb", closefd=False) as target:
                view = memoryview(content)
                while view:
                    written = target.write(view)
                    if written is None or written <= 0:
                        raise EvidenceFileError("unavailable")
                    view = view[written:]
                target.flush()
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            try:
                os.link(
                    temporary,
                    destination,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                current, digest = self._capture(relative, _MAX_MARKDOWN_BYTES)
                if digest != hashlib.sha256(content).hexdigest() or current != content:
                    raise EvidenceFileError("conflict")
            except OSError:
                raise EvidenceFileError("unavailable") from None
            else:
                os.fsync(parent_fd)
                current, digest = self._capture(relative, _MAX_MARKDOWN_BYTES)
                if digest != hashlib.sha256(content).hexdigest() or current != content:
                    raise EvidenceFileError("conflict")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.close(parent_fd)

    def _open_directory_chain(self, parts: Sequence[str]) -> int:
        """Open root and descendants by directory FD, refusing symlink traversal."""
        root = Path(self.vault.root)
        descriptor = os.open(root.anchor, _DIRECTORY_FLAGS)
        try:
            for part in (*root.parts[1:], *parts):
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError(errno.ENOTDIR, "not a directory")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino and stat.S_ISREG(right.st_mode)

    @classmethod
    def _same_stat(cls, left: os.stat_result, right: os.stat_result) -> bool:
        return (
            cls._same_file_identity(left, right)
            and left.st_mode == right.st_mode
            and left.st_size == right.st_size
            and left.st_mtime_ns == right.st_mtime_ns
            and left.st_ctime_ns == right.st_ctime_ns
        )
