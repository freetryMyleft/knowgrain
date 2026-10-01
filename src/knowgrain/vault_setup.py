"""Safe, persistent setup and selection for the application's single Vault."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import sys
import tempfile
import unicodedata
from typing import Any
from uuid import UUID

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase, VaultBindingConflict
from knowgrain.vault import VaultPathError, VaultStore
from knowgrain.wiki_files import WikiFileStore


VAULT_DIRECTORIES = (
    "Sources/Files",
    "Sources/Evidence",
    "Wiki/Drafts",
    "Wiki/Pages",
)


class VaultSetupError(RuntimeError):
    """Base class for errors safe to return from setup API routes."""


class VaultPathSetupError(VaultSetupError):
    pass


class VaultSelectionConflict(VaultSetupError):
    pass


class VaultDatabaseUnavailable(VaultSetupError):
    pass


class VaultSetupService:
    def __init__(self, settings: Settings, database: ApplicationDatabase) -> None:
        self.settings = settings
        self.database = database

    def configured_root(self) -> Path:
        return self._canonical_config_path(self.settings.vault_root, "configured Vault root")

    def allowed_parent(self) -> Path:
        parent = self._canonical_config_path(
            self.settings.vault_parent_dir, "configured Vault parent"
        )
        if parent.exists() and not parent.is_dir():
            raise VaultPathSetupError("The configured Vault parent is not a directory.")
        return parent

    async def initialize(self) -> tuple[VaultStore, dict[str, Any]]:
        """Adopt or reopen the database-selected Vault after DB readiness."""
        self._require_database()
        binding, source_count = await self.database.get_vault_state()
        if binding is None:
            root = self.configured_root()
            if source_count:
                originals = await self.database.list_source_originals()
                await self._verify_originals(root, originals)
            # Legacy files are fully checked before any folder is created.
            await self._prepare_root(root, create_root=True)
            try:
                binding = await self.database.compare_and_set_vault_binding(
                    expected_binding_id=None,
                    expected_root=str(root),
                    target_root=str(root),
                )
            except VaultBindingConflict as exc:
                raise VaultSelectionConflict(str(exc)) from exc
        else:
            root = self._canonical_bound_root(binding["root_path"])
            if source_count and not root.is_dir():
                raise VaultPathSetupError(
                    "The selected Vault folder is missing; restore it or repair the path before importing."
                )
            if source_count:
                originals = await self.database.list_source_originals()
                await self._verify_originals(root, originals)
            await self._prepare_root(root, create_root=not source_count)
        return VaultStore(root), binding

    async def status(
        self,
        *,
        ready: bool,
        detail: str | None,
    ) -> dict[str, Any]:
        self._require_database()
        binding, source_count = await self.database.get_vault_state()
        configured = self._display_path(self.settings.vault_root)
        parent = self._display_path(self.settings.vault_parent_dir)
        try:
            configured = self.configured_root()
        except VaultPathSetupError:
            pass
        try:
            parent = self.allowed_parent()
        except VaultPathSetupError:
            pass
        root = binding["root_path"] if binding else str(configured)
        directories: list[str] = []
        safe = True
        try:
            safe_root = (
                self._canonical_bound_root(binding["root_path"])
                if binding
                else self.configured_root()
            )
            root = str(safe_root)
            source_count += await self._wiki_content_count(safe_root)
            missing = self._inspect_directories(safe_root)
            directories = [directory for directory in VAULT_DIRECTORIES if directory not in missing]
            if missing:
                safe = False
                detail = detail or "One or more required Vault folders are missing."
        except VaultPathSetupError as exc:
            safe = False
            detail = detail or str(exc)
        return {
            "binding_id": str(binding["binding_id"]) if binding else None,
            "root": str(root),
            "configured_root": str(configured),
            "allowed_parent": str(parent),
            "ready": ready and safe,
            "selection_enabled": source_count == 0,
            "directories": directories,
            "detail": detail,
        }

    async def preview(self, name: str) -> dict[str, Any]:
        self._require_database()
        self._validate_name(name)
        binding, source_count = await self.database.get_vault_state()
        expected_root = (
            self._canonical_bound_root(binding["root_path"])
            if binding
            else self.configured_root()
        )
        candidate = self._candidate(name)
        directory_info = self._inspect_directories(candidate)
        same_root = candidate == expected_root
        source_count += await self._wiki_content_count(expected_root)
        if source_count and not same_root:
            raise VaultSelectionConflict("The Vault is locked because managed content already exists")
        return {
            "name": name,
            "root": str(candidate),
            "exists": candidate.exists(),
            "directories": list(VAULT_DIRECTORIES),
            "create_directories": directory_info or [],
            "selection_allowed": True,
            "binding_id": str(binding["binding_id"]) if binding else None,
            "expected_root": str(expected_root),
        }

    async def validate_selection(
        self,
        *,
        name: str,
        expected_binding_id: UUID | None,
        expected_root: str,
    ) -> tuple[Path, dict[str, Any] | None, int]:
        """Check CAS inputs and source locking before the runtime pauses its runner."""
        self._require_database()
        self._validate_name(name)
        binding, source_count = await self.database.get_vault_state()
        current_root = (
            self._canonical_bound_root(binding["root_path"])
            if binding
            else self.configured_root()
        )
        current_binding_id = binding["binding_id"] if binding else None
        try:
            parsed_expected_id = UUID(str(expected_binding_id)) if expected_binding_id else None
        except (ValueError, TypeError, AttributeError) as exc:
            raise VaultSelectionConflict("Vault selection changed; refresh the preview") from exc
        if parsed_expected_id != current_binding_id or expected_root != str(current_root):
            raise VaultSelectionConflict("Vault selection changed; refresh the preview")

        candidate = self._candidate(name)
        source_count += await self._wiki_content_count(current_root)
        if source_count and candidate != current_root:
            raise VaultSelectionConflict("The Vault is locked because managed content already exists")
        self._inspect_directories(candidate)
        return candidate, binding, source_count

    async def commit_selection(
        self,
        *,
        candidate: Path,
        expected_binding_id: UUID | None,
        expected_root: str,
        source_count: int,
    ) -> tuple[VaultStore, dict[str, Any]]:
        """Create intended directories, probe writes, and persist the CAS binding."""
        if str(candidate) != expected_root and await self._wiki_content_count(Path(expected_root)):
            raise VaultSelectionConflict("Wiki files changed; the current Vault is now locked")
        if source_count:
            await self._verify_originals(
                candidate, await self.database.list_source_originals()
            )
        await self._prepare_root(candidate, create_root=True)
        try:
            binding = await self.database.compare_and_set_vault_binding(
                expected_binding_id=expected_binding_id,
                expected_root=expected_root,
                target_root=str(candidate),
            )
        except VaultBindingConflict as exc:
            raise VaultSelectionConflict(str(exc)) from exc
        return VaultStore(candidate), binding

    async def _wiki_content_count(self, root: Path) -> int:
        """Conservatively protect on-disk pages before a watcher projects them.

        Files with invalid/duplicate IDs also lock the root until explicitly
        repaired; selecting away must not orphan content awaiting reconciliation.
        """
        if not root.exists():
            return 0
        try:
            scan = await asyncio.to_thread(WikiFileStore(VaultStore(root)).scan)
        except (OSError, ValueError) as exc:
            raise VaultPathSetupError("Cannot inspect Wiki files before selecting a Vault") from exc
        return len(scan.pages) + len(scan.issues)

    def _require_database(self) -> None:
        if not self.database.is_ready:
            detail = self.database.last_error or "Application database is unavailable or not migrated."
            raise VaultDatabaseUnavailable(detail)

    def _canonical_config_path(self, configured: Path, label: str) -> Path:
        raw = Path(configured).expanduser()
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        raw = self._normalize_trusted_system_prefix(raw)
        self._reject_symlinks(raw, label)
        current = Path(raw.anchor)
        for part in raw.parts[1:-1]:
            current = current / part
            if current.exists() and not current.is_dir():
                raise VaultPathSetupError(f"A path component of the {label} is not a directory.")
        try:
            canonical = raw.resolve(strict=False)
        except OSError as exc:
            raise VaultPathSetupError(f"Could not validate the {label}.") from exc
        if len(str(canonical)) > 4096:
            raise VaultPathSetupError(f"The {label} path is too long to store safely.")
        return canonical

    def _canonical_bound_root(self, stored_root: str) -> Path:
        raw = Path(stored_root).expanduser()
        if not raw.is_absolute():
            raise VaultPathSetupError("The stored Vault root is not an absolute path.")
        self._reject_symlinks(raw, "stored Vault root")
        try:
            canonical = raw.resolve(strict=False)
        except OSError as exc:
            raise VaultPathSetupError("Could not validate the selected Vault folder.") from exc
        if str(canonical) != stored_root:
            raise VaultPathSetupError("The stored Vault root is no longer canonical.")
        return canonical

    def _candidate(self, name: str) -> Path:
        parent = self.allowed_parent()
        candidate = parent / name
        # Check the user's selected component before resolving it. Resolving first
        # would make a symlink appear to be an ordinary path beneath the parent.
        self._reject_symlinks(candidate, "selected Vault folder")
        try:
            canonical = candidate.resolve(strict=False)
            canonical.relative_to(parent)
        except (OSError, ValueError) as exc:
            raise VaultPathSetupError("The selected folder must remain beneath the permitted parent.") from exc
        if canonical.parent != parent:
            raise VaultPathSetupError("The selected folder must be one folder beneath the permitted parent.")
        if len(str(canonical)) > 4096:
            raise VaultPathSetupError("The selected Vault folder path is too long.")
        if candidate.exists() and not candidate.is_dir():
            raise VaultPathSetupError("A file already exists at the selected Vault folder path.")
        self._inspect_directories(canonical)
        return canonical

    def _inspect_directories(self, root: Path) -> list[str]:
        if root.is_symlink():
            raise VaultPathSetupError("Symlink components are not allowed in Vault paths.")
        if root.exists() and not root.is_dir():
            raise VaultPathSetupError("A file already exists at the selected Vault folder path.")
        missing: list[str] = []
        for relative in VAULT_DIRECTORIES:
            candidate = root
            for part in PurePosixPath(relative).parts:
                candidate = candidate / part
                self._reject_symlinks(candidate, "Vault directory")
                if candidate.exists() and not candidate.is_dir():
                    raise VaultPathSetupError(
                        f"A file exists where the Vault folder '{relative}' is required."
                    )
            if not candidate.exists():
                missing.append(relative)
        return missing

    async def _prepare_root(self, root: Path, *, create_root: bool) -> None:
        try:
            missing = await asyncio.to_thread(self._prepare_root_sync, root, create_root)
        except VaultPathSetupError:
            raise
        except OSError as exc:
            raise VaultPathSetupError("The selected Vault folder could not be prepared.") from exc
        if missing:
            raise VaultPathSetupError("Some Vault folders could not be created.")

    @staticmethod
    def _prepare_root_sync(root: Path, create_root: bool) -> list[str]:
        VaultSetupService._reject_symlinks(root, "selected Vault folder")
        if root.exists() and not root.is_dir():
            raise VaultPathSetupError("A file already exists at the selected Vault folder path.")
        if not root.exists():
            if not create_root:
                raise VaultPathSetupError("The selected Vault folder does not exist.")
            root.mkdir(parents=True, exist_ok=True)
        VaultSetupService._reject_symlinks(root, "selected Vault folder")
        missing = []
        for relative in VAULT_DIRECTORIES:
            directory = root.joinpath(*PurePosixPath(relative).parts)
            VaultSetupService._reject_symlinks(directory, "Vault directory")
            if directory.exists() and not directory.is_dir():
                raise VaultPathSetupError(
                    f"A file exists where the Vault folder '{relative}' is required."
                )
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
            VaultSetupService._reject_symlinks(directory, "Vault directory")
            if not directory.is_dir():
                missing.append(relative)

        # Probe every application-owned directory that will be written by setup,
        # imports, or Wiki workflows. Only these uniquely named probe files are
        # removed; user files and .obsidian are never opened for writing.
        for relative in VAULT_DIRECTORIES:
            VaultSetupService._probe_directory(root.joinpath(*PurePosixPath(relative).parts))
        return missing

    @staticmethod
    def _probe_directory(directory: Path) -> None:
        probe_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=".knowgrain-write-probe-", delete=False
            ) as probe:
                probe_path = Path(probe.name)
                probe.write(b"knowgrain")
                probe.flush()
                os.fsync(probe.fileno())
        except OSError as exc:
            raise VaultPathSetupError("A required Vault folder is not writable.") from exc
        finally:
            if probe_path is not None:
                try:
                    probe_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise VaultPathSetupError("A Vault write check could not be cleaned up.") from exc

    async def _verify_originals(self, root: Path, originals: list[tuple[str, str]]) -> None:
        await asyncio.to_thread(self._verify_originals_sync, root, originals)

    @staticmethod
    def _verify_originals_sync(root: Path, originals: list[tuple[str, str]]) -> None:
        vault = VaultStore(root)
        try:
            for relative_path, expected_digest in originals:
                path = vault.resolve(relative_path)
                if not _valid_digest(expected_digest):
                    raise VaultPathSetupError(
                        "A stored source record has an invalid SHA-256 digest; Vault adoption is blocked."
                    )
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise VaultPathSetupError(
                            "A stored source original is not a regular file; Vault adoption is blocked."
                        )
                    digest = hashlib.sha256()
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk)
                    if digest.hexdigest() != expected_digest:
                        raise VaultPathSetupError(
                            "A stored source original is missing or its SHA-256 does not match; Vault adoption is blocked."
                        )
                finally:
                    os.close(descriptor)
        except FileNotFoundError as exc:
            raise VaultPathSetupError(
                "A stored source original is missing; Vault adoption is blocked."
            ) from exc
        except (VaultPathError, OSError) as exc:
            if isinstance(exc, VaultPathSetupError):
                raise
            raise VaultPathSetupError(
                "A stored source original could not be safely verified; Vault adoption is blocked."
            ) from exc

    @staticmethod
    def _reject_symlinks(path: Path, label: str) -> None:
        raw = Path(path)
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        raw = VaultSetupService._normalize_trusted_system_prefix(raw)
        current = Path(raw.anchor)
        for part in raw.parts[1:]:
            current = current / part
            try:
                if current.is_symlink():
                    raise VaultPathSetupError(f"Symlink components are not allowed in the {label}.")
            except OSError as exc:
                raise VaultPathSetupError(f"Could not validate the {label}.") from exc

    @staticmethod
    def _normalize_trusted_system_prefix(path: Path) -> Path:
        """Normalize only macOS' fixed /tmp, /var, and /etc system aliases."""
        if sys.platform != "darwin":
            return path
        value = str(path)
        for alias, canonical in (
            ("/tmp", "/private/tmp"),
            ("/var", "/private/var"),
            ("/etc", "/private/etc"),
        ):
            if value == alias or value.startswith(alias + "/"):
                return Path(canonical + value[len(alias):])
        return path

    @staticmethod
    def _display_path(path: Path) -> Path:
        path = Path(path).expanduser()
        return path if path.is_absolute() else Path.cwd() / path

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name or len(name) > 120:
            raise VaultPathSetupError("Enter a folder name between 1 and 120 characters.")
        if (
            name in {".", ".."}
            or "/" in name
            or "\\" in name
            or ":" in name
            or name.endswith((" ", "."))
            or any(unicodedata.category(character) == "Cc" for character in name)
            or PurePosixPath(name).is_absolute()
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
            or PurePosixPath(name).parts != (name,)
        ):
            raise VaultPathSetupError("Enter one safe folder name without path syntax.")


def _valid_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
