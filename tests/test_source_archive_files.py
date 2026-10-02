from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from knowgrain.source_archive_files import (
    ArchiveEntry,
    SourceArchiveFileError,
    SourceArchiveFiles,
)
from knowgrain.vault import VaultStore


class SourceArchiveFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "vault"
        self.vault = VaultStore(self.root)
        self.vault.initialize()
        self.files = SourceArchiveFiles(self.vault)
        self.source_id = uuid4()

    def _entries(self, values: tuple[tuple[str, bytes], ...]) -> tuple[ArchiveEntry, ...]:
        entries: list[ArchiveEntry] = []
        for suffix, content in values:
            revision_id = uuid4()
            path = self.vault.write_source(
                self.source_id, revision_id, f"source{suffix}", content
            )
            entries.append(ArchiveEntry(revision_id, path, hashlib.sha256(content).hexdigest()))
        return tuple(entries)

    def _path(self, entry: ArchiveEntry) -> Path:
        return self.vault.resolve(entry.vault_path)

    def test_archives_and_restores_all_revisions_as_one_directory(self) -> None:
        contents = ((".txt", b"first revision"), (".md", b"second revision"))
        entries = self._entries(contents)
        source_dir = self._path(entries[0]).parent
        trash_dir = self.root / "Trash" / "Files" / str(self.source_id)

        self.assertEqual(self.files.location(self.source_id, entries), "vault")
        self.files.archive(self.source_id, entries)
        self.assertFalse(source_dir.exists())
        self.assertEqual(self.files.location(self.source_id, entries), "trash")
        self.assertEqual(
            [ (trash_dir / Path(entry.vault_path).name).read_bytes() for entry in entries ],
            [content for _, content in contents],
        )

        # Same-direction retries verify the complete directory and remain safe.
        self.files.archive(self.source_id, entries)
        self.files.restore(self.source_id, entries)
        self.assertEqual(self.files.location(self.source_id, entries), "vault")
        self.files.restore(self.source_id, entries)
        self.assertEqual(
            [self._path(entry).read_bytes() for entry in entries],
            [content for _, content in contents],
        )

    def test_hash_mismatch_and_missing_registered_member_prevent_any_move(self) -> None:
        entries = self._entries(((".txt", b"one"), (".pdf", b"two")))
        self._path(entries[1]).write_bytes(b"changed")

        with self.assertRaises(SourceArchiveFileError) as error:
            self.files.archive(self.source_id, entries)
        self.assertEqual(error.exception.code, "conflict")
        self.assertTrue(self._path(entries[0]).exists())
        self.assertTrue(self._path(entries[1]).exists())
        self.assertFalse((self.root / "Trash" / "Files" / str(self.source_id)).exists())

        self._path(entries[1]).unlink()
        with self.assertRaises(SourceArchiveFileError) as missing:
            self.files.archive(self.source_id, entries)
        self.assertEqual(missing.exception.code, "conflict")
        self.assertTrue(self._path(entries[0]).exists())

    def test_unknown_members_symlinks_and_fifo_are_rejected(self) -> None:
        entry, = self._entries(((".txt", b"registered"),))
        source_dir = self._path(entry).parent
        (source_dir / "notes.txt").write_text("unregistered")
        with self.assertRaises(SourceArchiveFileError) as extra:
            self.files.archive(self.source_id, (entry,))
        self.assertEqual(extra.exception.code, "conflict")
        (source_dir / "notes.txt").unlink()

        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(b"registered")
        original_path = source_dir / Path(entry.vault_path).name
        original_path.unlink()
        original_path.symlink_to(outside)
        with self.assertRaises(SourceArchiveFileError) as symlink:
            self.files.archive(self.source_id, (entry,))
        self.assertEqual(symlink.exception.code, "conflict")

        original_path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(original_path)
            with self.assertRaises(SourceArchiveFileError) as fifo:
                self.files.archive(self.source_id, (entry,))
            self.assertEqual(fifo.exception.code, "conflict")

    def test_both_source_and_trash_directories_are_a_conflict(self) -> None:
        entry, = self._entries(((".txt", b"registered"),))
        source_dir = self._path(entry).parent
        trash_dir = self.root / "Trash" / "Files" / str(self.source_id)
        trash_dir.parent.mkdir(parents=True, exist_ok=True)
        trash_dir.mkdir()
        (trash_dir / Path(entry.vault_path).name).write_bytes(b"registered")

        with self.assertRaises(SourceArchiveFileError) as error:
            self.files.archive(self.source_id, (entry,))
        self.assertEqual(error.exception.code, "conflict")
        self.assertTrue((source_dir / Path(entry.vault_path).name).exists())
        self.assertTrue((trash_dir / Path(entry.vault_path).name).exists())

    def test_destination_created_after_preflight_is_never_replaced(self) -> None:
        entry, = self._entries(((".txt", b"registered"),))
        source_dir = self._path(entry).parent
        trash_dir = self.root / "Trash" / "Files" / str(self.source_id)
        trash_dir.parent.mkdir(parents=True, exist_ok=True)
        real_rename = SourceArchiveFiles._rename_directory_exclusive

        def create_collision_then_rename(source_parent_fd, source_name, target_parent_fd, target_name):
            trash_dir.mkdir()
            marker = trash_dir / "keep.txt"
            marker.write_bytes(b"do not replace")
            real_rename(source_parent_fd, source_name, target_parent_fd, target_name)

        with patch.object(
            SourceArchiveFiles,
            "_rename_directory_exclusive",
            side_effect=create_collision_then_rename,
        ):
            with self.assertRaises(SourceArchiveFileError) as error:
                self.files.archive(self.source_id, (entry,))

        self.assertEqual(error.exception.code, "conflict")
        self.assertTrue((source_dir / Path(entry.vault_path).name).exists())
        self.assertEqual((trash_dir / "keep.txt").read_bytes(), b"do not replace")

    def test_missing_source_is_distinguished_from_missing_member(self) -> None:
        entry, = self._entries(((".txt", b"registered"),))
        self._path(entry).parent.rename(self._path(entry).parent.with_name("parked"))
        with self.assertRaises(SourceArchiveFileError) as error:
            self.files.archive(self.source_id, (entry,))
        self.assertEqual(error.exception.code, "missing")

    def test_post_move_fsync_failure_leaves_safe_idempotent_retry(self) -> None:
        entry, = self._entries(((".txt", b"registered"),))
        (self.root / "Trash" / "Files").mkdir(parents=True, exist_ok=True)
        real_fsync = os.fsync
        real_rename = SourceArchiveFiles._rename_directory_exclusive
        moved = False
        failed = False

        def move_then_mark(source_parent_fd, source_name, target_parent_fd, target_name):
            nonlocal moved
            real_rename(source_parent_fd, source_name, target_parent_fd, target_name)
            moved = True

        def fail_first_fsync_after_move(descriptor: int) -> None:
            nonlocal failed
            if moved and not failed:
                failed = True
                raise OSError("simulated directory fsync failure")
            real_fsync(descriptor)

        with (
            patch.object(
                SourceArchiveFiles,
                "_rename_directory_exclusive",
                side_effect=move_then_mark,
            ),
            patch("knowgrain.source_archive_files.os.fsync", side_effect=fail_first_fsync_after_move),
        ):
            with self.assertRaises(SourceArchiveFileError) as error:
                self.files.archive(self.source_id, (entry,))
        self.assertEqual(error.exception.code, "unavailable")

        trash_path = self.root / "Trash" / "Files" / str(self.source_id) / Path(entry.vault_path).name
        self.assertTrue(trash_path.exists())
        self.assertEqual(trash_path.read_bytes(), b"registered")
        # Retry must sync the canonical Sources/Files parent too, even
        # though the original directory is already at its destination.
        synced_paths = []
        source_parent_identity = os.stat(self.root / "Sources" / "Files")
        def capture_sync(descriptor):
            opened = os.fstat(descriptor)
            synced_paths.append((opened.st_dev, opened.st_ino))
            real_fsync(descriptor)
        with patch("knowgrain.source_archive_files.os.fsync", side_effect=capture_sync):
            self.files.archive(self.source_id, (entry,))
        self.assertIn((source_parent_identity.st_dev, source_parent_identity.st_ino), synced_paths)
        self.assertEqual(self.files.location(self.source_id, (entry,)), "trash")

    def test_oversized_original_is_rejected_before_hashing(self):
        entry, = self._entries(((".txt", b"registered"),))
        with patch("knowgrain.source_archive_files.MAX_UPLOAD_BYTES", 5):
            with self.assertRaises(SourceArchiveFileError) as error:
                self.files.archive(self.source_id, (entry,))
        self.assertEqual(error.exception.code, "conflict")
        self.assertTrue(self._path(entry).exists())

    def test_new_directory_fsync_failure_closes_every_open_descriptor(self):
        # Exercise actual mkdir/open, with only the new child's fsync failing.
        self.root.joinpath("Trash/Files").rmdir()
        self.root.joinpath("Trash").rmdir()
        entry, = self._entries(((".txt", b"registered"),))
        real_open, real_close, real_fsync = os.open, os.close, os.fsync
        owned = set()
        created_child = None
        def track_open(path, *args, **kwargs):
            nonlocal created_child
            fd = real_open(path, *args, **kwargs)
            owned.add(fd)
            if path == "Trash":
                created_child = fd
            return fd
        def track_close(fd):
            real_close(fd)
            owned.remove(fd)
        def fail_child_sync(fd):
            if fd == created_child:
                raise OSError("injected new-directory durability failure")
            real_fsync(fd)
        with (
            patch("knowgrain.source_archive_files.os.open", side_effect=track_open),
            patch("knowgrain.source_archive_files.os.close", side_effect=track_close),
            patch("knowgrain.source_archive_files.os.fsync", side_effect=fail_child_sync),
            patch("knowgrain.source_archive_files.os.supports_dir_fd", {os.open, os.stat, os.mkdir}),
        ):
            with self.assertRaises(SourceArchiveFileError) as error:
                self.files.archive(self.source_id, (entry,))
        self.assertEqual(error.exception.code, "unavailable")
        self.assertIsNotNone(created_child)
        self.assertEqual(owned, set())
        self.assertTrue(self._path(entry).exists())

    def test_invalid_entries_and_canonical_paths_are_rejected(self) -> None:
        entry, = self._entries(((".txt", b"registered"),))
        invalid = ArchiveEntry(entry.revision_id, entry.vault_path.replace(".txt", ".TXT"), entry.sha256)
        for values in (("not entries"), (invalid,), (entry, entry)):
            with self.subTest(values=values):
                with self.assertRaises(SourceArchiveFileError) as error:
                    self.files.archive(self.source_id, values)  # type: ignore[arg-type]
                self.assertEqual(error.exception.code, "unavailable")


if __name__ == "__main__":
    unittest.main()
