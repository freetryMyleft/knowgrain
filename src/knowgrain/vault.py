"""Safe, immutable access to the source files stored in a Knowgrain Vault."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tempfile
from uuid import UUID


class VaultPathError(ValueError):
    """A relative Vault path is invalid or crosses a symlink."""


class VaultConflictError(FileExistsError):
    """An immutable source revision already exists with different bytes."""


class VaultStore:
    """Read and write source files beneath one configured Vault directory.

    Paths returned to the rest of the application are Vault-relative POSIX paths.
    Source revisions are immutable once published.
    """

    _DIRECTORIES = (
        "Sources/Files",
        "Sources/Evidence",
        "Wiki/Drafts",
        "Wiki/Pages",
        "Trash/Files",
    )

    def __init__(self, root: Path) -> None:
        # Resolve the configured root once. All path checks below are then scoped
        # to its canonical location while symlinks inside the Vault are rejected.
        self.root = Path(root).expanduser().resolve()

    def initialize(self) -> None:
        """Create the intended Knowgrain folders without altering existing data."""
        self._reject_root_symlink()
        self.root.mkdir(parents=True, exist_ok=True)
        for relative_path in self._DIRECTORIES:
            directory = self.resolve(relative_path)
            directory.mkdir(parents=True, exist_ok=True)
            # Re-check after mkdir so an existing symlink is never accepted.
            self.resolve(relative_path)

    def resolve(self, relative_path: str) -> Path:
        """Return a checked Vault path, rejecting absolute, traversing, or symlink paths."""
        if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
            raise VaultPathError("Vault path must be a non-empty relative path")
        if "\\" in relative_path:
            raise VaultPathError("Vault paths must use forward slashes")

        posix_path = PurePosixPath(relative_path)
        windows_path = PureWindowsPath(relative_path)
        if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise VaultPathError("absolute Vault paths are not allowed")
        if any(part in {".", ".."} for part in posix_path.parts):
            raise VaultPathError("Vault path traversal is not allowed")
        if not posix_path.parts:
            raise VaultPathError("Vault path must name a file or directory")

        candidate = self.root.joinpath(*posix_path.parts)
        self._reject_symlink_components(candidate)
        return candidate

    def write_source(
        self,
        source_id: UUID,
        revision_id: UUID,
        filename: str,
        content: bytes,
    ) -> str:
        """Atomically publish an immutable original and return its Vault path."""
        suffix = self._normalized_suffix(filename)
        relative_path = f"Sources/Files/{source_id}/{revision_id}{suffix}"
        destination = self.resolve(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(destination)

        try:
            existing = destination.read_bytes()
        except FileNotFoundError:
            pass
        else:
            if existing == content:
                return relative_path
            raise VaultConflictError(f"source revision already exists: {relative_path}")

        # Write a complete sibling first, then publish with a hard link. Linking
        # fails atomically if another writer already created the destination.
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".knowgrain-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())

            self._reject_symlink_components(destination)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if destination.is_symlink():
                    raise VaultPathError("symlink components are not allowed in Vault paths")
                try:
                    existing = destination.read_bytes()
                except FileNotFoundError:
                    raise
                if existing == content:
                    return relative_path
                raise VaultConflictError(f"source revision already exists: {relative_path}")

            # Make the directory entry durable where the platform supports it.
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
            return relative_path
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def read_bytes(self, relative_path: str) -> bytes:
        """Read a regular original without blocking on an unexpected file node."""
        path = self.resolve(relative_path)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VaultPathError("Vault originals must be regular files")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                return source.read()
        finally:
            os.close(descriptor)

    @staticmethod
    def _normalized_suffix(filename: str) -> str:
        if not isinstance(filename, str) or not filename:
            raise ValueError("filename must be a non-empty string")
        suffix = PureWindowsPath(filename).suffix or PurePosixPath(filename).suffix
        suffix = suffix.casefold()
        if suffix and not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            raise ValueError("filename has an invalid extension")
        return suffix

    def _reject_symlink_components(self, candidate: Path) -> None:
        self._reject_root_symlink()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise VaultPathError("Vault path escapes the configured root") from exc

        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                is_symlink = current.is_symlink()
            except OSError as exc:
                raise VaultPathError("could not validate Vault path") from exc
            if is_symlink:
                raise VaultPathError("symlink components are not allowed in Vault paths")

    def _reject_root_symlink(self) -> None:
        # The root was canonicalized at construction. A later ancestor rename
        # followed by a symlink must not redirect this store to another tree.
        current = Path(self.root.anchor)
        try:
            for part in self.root.parts[1:]:
                current = current / part
                if current.is_symlink():
                    raise VaultPathError("configured Vault root or its ancestors cannot become symlinks")
            if self.root.resolve(strict=False) != self.root:
                raise VaultPathError("configured Vault root is no longer canonical")
        except (OSError, RuntimeError) as exc:
            raise VaultPathError("could not validate the canonical Vault root") from exc
