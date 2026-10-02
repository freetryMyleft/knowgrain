"""Move verified source revision directories into and out of Vault trash.

This layer deliberately knows nothing about database lifecycle state. Callers
must persist their intent and hold the source file lock around these methods.
Directory moves use the operating system's exclusive rename primitive where it
is available (Linux ``renameat2`` and macOS ``renameatx_np``). On systems with
no dir-fd support or no exclusive directory rename, mutations fail closed with
``unavailable`` rather than risk replacing a destination directory.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Literal
from uuid import UUID

from knowgrain.vault import VaultPathError, VaultStore
from knowgrain.config import MAX_UPLOAD_BYTES


_SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf", ".docx"}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ArchiveEntry:
    """A registered source revision and its canonical immutable Vault path."""

    revision_id: UUID
    vault_path: str
    sha256: str


class SourceArchiveFileError(RuntimeError):
    """A source archive operation is missing, conflicts, or is unavailable."""

    def __init__(self, code: Literal["missing", "conflict", "unavailable"]) -> None:
        if code not in {"missing", "conflict", "unavailable"}:
            raise ValueError("invalid source archive file error code")
        self.code: Literal["missing", "conflict", "unavailable"] = code
        super().__init__(
            {
                "missing": "Source archive directory is missing.",
                "conflict": "Source archive files differ from their registered revisions.",
                "unavailable": "Source archive files cannot be accessed safely.",
            }[code]
        )


class _DirectoryChain:
    """Pinned directory descriptors whose names can be checked after a race."""

    def __init__(self, descriptors: list[int], names: list[str], root_path: Path) -> None:
        self.descriptors = descriptors
        self.names = names
        self.root_path = root_path

    @property
    def fd(self) -> int:
        return self.descriptors[-1]

    def verify(self) -> None:
        for index, name in enumerate(self.names):
            parent_fd = self.descriptors[index]
            child_fd = self.descriptors[index + 1]
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise _UnsafePath from exc
            if not _same_directory_identity(os.fstat(child_fd), named):
                raise _UnsafePath

        try:
            rooted = os.stat(self.root_path, follow_symlinks=False)
        except OSError as exc:
            raise _UnsafePath from exc
        root_fd_index = len(self.root_path.parts) - 1
        if not _same_directory_identity(os.fstat(self.descriptors[root_fd_index]), rooted):
            raise _UnsafePath

    def __enter__(self) -> _DirectoryChain:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


class _UnsafePath(Exception):
    pass


class SourceArchiveFiles:
    """Safely archive and restore complete source-revision directories."""

    def __init__(self, vault: VaultStore) -> None:
        if not isinstance(vault, VaultStore):
            raise TypeError("SourceArchiveFiles requires a VaultStore")
        self.vault = vault

    def archive(self, source_id: UUID, entries: Sequence[ArchiveEntry]) -> None:
        self._move(source_id, entries, to_trash=True)

    def restore(self, source_id: UUID, entries: Sequence[ArchiveEntry]) -> None:
        self._move(source_id, entries, to_trash=False)

    def location(self, source_id: UUID, entries: Sequence[ArchiveEntry]) -> Literal["vault", "trash"]:
        source_id, checked = self._validate(source_id, entries)
        try:
            with self._open_parent(("Sources", "Files")) as source_parent:
                try:
                    trash_parent = self._open_parent(("Trash", "Files"), create=False)
                except FileNotFoundError:
                    source_exists = self._directory_identity(source_parent.fd, str(source_id)) is not None
                    if not source_exists:
                        raise SourceArchiveFileError("missing")
                    self._verify_and_close_named_source_directory(
                        source_parent.fd, str(source_id), checked
                    )
                    source_parent.verify()
                    return "vault"
                with trash_parent:
                    return self._location_with_parents(source_id, checked, source_parent, trash_parent)
        except SourceArchiveFileError:
            raise
        except FileNotFoundError:
            raise SourceArchiveFileError("missing") from None
        except (_UnsafePath, VaultPathError):
            raise SourceArchiveFileError("conflict") from None
        except (OSError, ValueError):
            raise SourceArchiveFileError("unavailable") from None

    def _location_with_parents(
        self,
        source_id: UUID,
        checked: Sequence[ArchiveEntry],
        source_parent: _DirectoryChain,
        trash_parent: _DirectoryChain,
    ) -> Literal["vault", "trash"]:
        source_exists = self._directory_identity(source_parent.fd, str(source_id)) is not None
        trash_exists = self._directory_identity(trash_parent.fd, str(source_id)) is not None
        if source_exists and trash_exists:
            raise SourceArchiveFileError("conflict")
        if not source_exists and not trash_exists:
            raise SourceArchiveFileError("missing")

        parent = source_parent if source_exists else trash_parent
        self._verify_and_close_named_source_directory(parent.fd, str(source_id), checked)
        source_parent.verify()
        trash_parent.verify()
        return "vault" if source_exists else "trash"

    def _move(
        self,
        source_id: UUID,
        entries: Sequence[ArchiveEntry],
        *,
        to_trash: bool,
    ) -> None:
        source_id, checked = self._validate(source_id, entries)
        source_components = ("Sources", "Files")
        trash_components = ("Trash", "Files")
        try:
            # The source path is established by VaultStore.initialize(). Trash
            # parents are created one component at a time through pinned FDs.
            with self._open_parent(source_components) as source_parent, self._open_parent(
                trash_components, create=to_trash
            ) as trash_parent:
                start_parent, end_parent = (
                    (source_parent, trash_parent) if to_trash else (trash_parent, source_parent)
                )
                source_name = str(source_id)
                source_identity = self._directory_identity(source_parent.fd, source_name)
                trash_identity = self._directory_identity(trash_parent.fd, source_name)

                if source_identity is not None and trash_identity is not None:
                    raise SourceArchiveFileError("conflict")
                if source_identity is None and trash_identity is None:
                    raise SourceArchiveFileError("missing")

                start_identity = source_identity if to_trash else trash_identity
                end_identity = trash_identity if to_trash else source_identity
                if start_identity is None:
                    # Idempotent retry: the exact full set is already at the
                    # requested destination. Recheck both parent chains first.
                    self._verify_and_close_named_source_directory(
                        end_parent.fd, source_name, checked
                    )
                    source_parent.verify()
                    trash_parent.verify()
                    # A previous move may have succeeded before its parent
                    # fsync failed. Replay must establish durability too.
                    os.fsync(start_parent.fd)
                    os.fsync(end_parent.fd)
                    return
                if end_identity is not None:
                    raise SourceArchiveFileError("conflict")

                source_dir_fd = self._verify_named_source_directory(start_parent.fd, source_name, checked)
                try:
                    source_dir_identity = os.fstat(source_dir_fd)
                    start_parent.verify()
                    end_parent.verify()
                    if not _same_directory_identity(
                        source_dir_identity,
                        os.stat(source_name, dir_fd=start_parent.fd, follow_symlinks=False),
                    ):
                        raise SourceArchiveFileError("conflict")

                    self._rename_directory_exclusive(
                        start_parent.fd, source_name, end_parent.fd, source_name
                    )

                    # A successful rename is not reported until its named
                    # destination, full member set, and both parent chains are
                    # revalidated. Failures leave the moved files in place so
                    # the journal owner can retry against their actual location.
                    source_parent.verify()
                    end_parent.verify()
                    if self._directory_identity(start_parent.fd, source_name) is not None:
                        raise SourceArchiveFileError("conflict")
                    moved_fd = self._verify_named_source_directory(end_parent.fd, source_name, checked)
                    try:
                        if not _same_directory_identity(source_dir_identity, os.fstat(moved_fd)):
                            raise SourceArchiveFileError("conflict")
                    finally:
                        os.close(moved_fd)

                    os.fsync(start_parent.fd)
                    os.fsync(end_parent.fd)
                finally:
                    os.close(source_dir_fd)
        except SourceArchiveFileError:
            raise
        except FileNotFoundError:
            # Missing registered members inside an existing source directory
            # are detected by the complete-set verifier as conflicts.
            raise SourceArchiveFileError("missing") from None
        except (_UnsafePath, VaultPathError):
            raise SourceArchiveFileError("conflict") from None
        except (OSError, ValueError):
            raise SourceArchiveFileError("unavailable") from None

    def _validate(
        self, source_id: UUID, entries: Sequence[ArchiveEntry]
    ) -> tuple[UUID, tuple[ArchiveEntry, ...]]:
        if not isinstance(source_id, UUID):
            raise SourceArchiveFileError("unavailable")
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            raise SourceArchiveFileError("unavailable")
        checked = tuple(entries)
        if not 1 <= len(checked) <= 10_000:
            raise SourceArchiveFileError("unavailable")
        seen_revisions: set[UUID] = set()
        for entry in checked:
            if not isinstance(entry, ArchiveEntry) or not isinstance(entry.revision_id, UUID):
                raise SourceArchiveFileError("unavailable")
            if entry.revision_id in seen_revisions:
                raise SourceArchiveFileError("unavailable")
            seen_revisions.add(entry.revision_id)
            if not isinstance(entry.sha256, str) or not _SHA256_PATTERN.fullmatch(entry.sha256):
                raise SourceArchiveFileError("unavailable")
            if not isinstance(entry.vault_path, str):
                raise SourceArchiveFileError("unavailable")
            prefix = f"Sources/Files/{source_id}/{entry.revision_id}"
            if not entry.vault_path.startswith(prefix):
                raise SourceArchiveFileError("unavailable")
            suffix = entry.vault_path[len(prefix) :]
            if suffix not in _SUPPORTED_SUFFIXES:
                raise SourceArchiveFileError("unavailable")
            expected_path = f"{prefix}{suffix}"
            if entry.vault_path != expected_path or PurePosixPath(entry.vault_path).as_posix() != expected_path:
                raise SourceArchiveFileError("unavailable")
            try:
                self.vault.resolve(expected_path)
            except (VaultPathError, OSError, ValueError):
                # A symlink at a managed directory/member path is an integrity
                # conflict. Avoid telling a caller that an arbitrary path is valid.
                raise SourceArchiveFileError("conflict") from None
        return source_id, checked

    def _open_parent(self, components: Sequence[str], *, create: bool = False) -> _DirectoryChain:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.mkdir not in os.supports_dir_fd
            or os.listdir not in os.supports_fd
        ):
            raise OSError(errno.ENOTSUP, "safe dir-fd operations are unavailable")
        root_path = Path(self.vault.root)
        flags = _DIRECTORY_FLAGS
        descriptors: list[int] = []
        names: list[str] = []
        try:
            descriptor = os.open(root_path.anchor or "/", flags)
            descriptors.append(descriptor)
            # Resolve the configured root from the filesystem anchor without
            # following any symlink component, then continue below that root.
            root_components = root_path.parts[1:]
            for index, component in enumerate((*root_components, *components)):
                if component in {"", ".", ".."} or "/" in component:
                    raise _UnsafePath
                parent_fd = descriptors[-1]
                created = False
                try:
                    named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if not create or index < len(root_components):
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    child = os.open(component, flags, dir_fd=parent_fd)
                    created = True
                else:
                    if not stat.S_ISDIR(named.st_mode):
                        raise _UnsafePath
                    try:
                        child = os.open(component, flags, dir_fd=parent_fd)
                    except OSError as exc:
                        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                            raise _UnsafePath from exc
                        raise
                # Register ownership before any syscall on the new descriptor
                # can fail, so the exception path always closes it.
                descriptors.append(child)
                names.append(component)
                if created:
                    os.fsync(child)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise _UnsafePath
            chain = _DirectoryChain(descriptors, names, root_path)
            chain.verify()
            if root_path.resolve(strict=True) != root_path:
                raise _UnsafePath
            if create:
                # On retry, a previous mkdir may have succeeded before its
                # parent fsync failed. Sync the full target-parent chain each
                # time so an archive never relies on an undurable directory.
                root_fd_index = len(root_path.parts) - 1
                for directory_fd in descriptors[root_fd_index:]:
                    os.fsync(directory_fd)
            return chain
        except BaseException:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def _directory_identity(self, parent_fd: int, name: str) -> os.stat_result | None:
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(named.st_mode):
            raise SourceArchiveFileError("conflict")
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not _same_directory_identity(named, opened):
                raise SourceArchiveFileError("conflict")
            return opened
        finally:
            os.close(descriptor)

    def _verify_named_source_directory(
        self, parent_fd: int, name: str, entries: Sequence[ArchiveEntry]
    ) -> int:
        try:
            named_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise SourceArchiveFileError("missing") from None
        if not stat.S_ISDIR(named_before.st_mode):
            raise SourceArchiveFileError("conflict")
        try:
            directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise SourceArchiveFileError("conflict") from None
            raise
        try:
            directory_before = os.fstat(directory_fd)
            if not _same_directory_identity(named_before, directory_before):
                raise SourceArchiveFileError("conflict")

            expected: dict[str, str] = {}
            for entry in entries:
                expected[entry.vault_path.rsplit("/", 1)[1]] = entry.sha256
            try:
                names = os.listdir(directory_fd)
            except OSError:
                raise
            if len(names) != len(expected) or set(names) != set(expected):
                raise SourceArchiveFileError("conflict")

            for filename, expected_hash in expected.items():
                self._verify_member(directory_fd, filename, expected_hash)

            directory_after = os.fstat(directory_fd)
            try:
                named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise SourceArchiveFileError("conflict") from None
            if not _same_directory_stat(directory_before, directory_after) or not _same_directory_identity(
                directory_after, named_after
            ):
                raise SourceArchiveFileError("conflict")
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    def _verify_and_close_named_source_directory(
        self, parent_fd: int, name: str, entries: Sequence[ArchiveEntry]
    ) -> None:
        directory_fd = self._verify_named_source_directory(parent_fd, name, entries)
        os.close(directory_fd)

    @staticmethod
    def _verify_member(directory_fd: int, filename: str, expected_hash: str) -> None:
        try:
            before_path = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise SourceArchiveFileError("conflict") from None
        if not stat.S_ISREG(before_path.st_mode):
            raise SourceArchiveFileError("conflict")
        try:
            descriptor = os.open(filename, _FILE_FLAGS, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENXIO}:
                raise SourceArchiveFileError("conflict") from None
            raise
        try:
            before_fd = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before_fd.st_mode)
                or not _same_file_identity(before_fd, before_path)
                or before_fd.st_size > MAX_UPLOAD_BYTES
            ):
                raise SourceArchiveFileError("conflict")
            digest = hashlib.sha256()
            captured = 0
            while True:
                block = os.read(descriptor, min(_HASH_CHUNK_BYTES, MAX_UPLOAD_BYTES - captured + 1))
                if not block:
                    break
                captured += len(block)
                if captured > MAX_UPLOAD_BYTES:
                    raise SourceArchiveFileError("conflict")
                digest.update(block)
            after_fd = os.fstat(descriptor)
            try:
                after_path = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise SourceArchiveFileError("conflict") from None
            if (
                not _same_file_stat(before_fd, after_fd)
                or not _same_file_identity(after_fd, after_path)
                or digest.hexdigest() != expected_hash
            ):
                raise SourceArchiveFileError("conflict")
        finally:
            os.close(descriptor)

    @staticmethod
    def _rename_directory_exclusive(
        source_parent_fd: int, source_name: str, destination_parent_fd: int, destination_name: str
    ) -> None:
        """Rename a directory atomically without replacing any destination."""
        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                raise OSError(errno.ENOTSUP, "exclusive renameat2 is unavailable")
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_parent_fd,
                os.fsencode(source_name),
                destination_parent_fd,
                os.fsencode(destination_name),
                1,  # RENAME_NOREPLACE
            )
        elif sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            renameatx_np = getattr(libc, "renameatx_np", None)
            if renameatx_np is None:
                raise OSError(errno.ENOTSUP, "exclusive renameatx_np is unavailable")
            renameatx_np.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx_np.restype = ctypes.c_int
            result = renameatx_np(
                source_parent_fd,
                os.fsencode(source_name),
                destination_parent_fd,
                os.fsencode(destination_name),
                0x00000004,  # RENAME_EXCL
            )
        else:
            raise OSError(errno.ENOTSUP, "exclusive directory rename is unavailable")

        if result != 0:
            error_number = ctypes.get_errno()
            if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
                raise SourceArchiveFileError("conflict")
            raise OSError(error_number, os.strerror(error_number))


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _same_directory_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_directory_identity(left, right)
        and left.st_mode == right.st_mode
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _same_file_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_file_identity(left, right)
        and left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )
