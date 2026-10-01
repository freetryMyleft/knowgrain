from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from uuid import UUID

from knowgrain.vault import VaultConflictError, VaultPathError, VaultStore


class VaultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "vault"
        self.vault = VaultStore(self.root)
        self.source_id = UUID("6e35247c-9253-45f8-b8df-c9fac2bc3308")
        self.revision_id = UUID("17e1bf0d-46c4-43cc-9c21-435eeb244257")

    def test_initialize_creates_only_intended_vault_folders_and_preserves_obsidian(self) -> None:
        obsidian = self.root / ".obsidian"
        obsidian.mkdir(parents=True)
        config = obsidian / "app.json"
        config.write_text('{"keep": true}', encoding="utf-8")

        self.vault.initialize()

        self.assertTrue((self.root / "Sources/Files").is_dir())
        self.assertTrue((self.root / "Sources/Evidence").is_dir())
        self.assertTrue((self.root / "Wiki/Drafts").is_dir())
        self.assertTrue((self.root / "Wiki/Pages").is_dir())
        self.assertEqual(config.read_text(encoding="utf-8"), '{"keep": true}')
        self.assertFalse((self.root / ".knowgrain").exists())

    def test_replaced_root_ancestor_cannot_redirect_reads_or_writes(self) -> None:
        base = Path(self.temporary_directory.name)
        holder = base / "holder"
        original_root = holder / "vault"
        original = VaultStore(original_root)
        original.initialize()
        outside = base / "outside"
        outside_vault = VaultStore(outside / "vault")
        outside_vault.initialize()
        path = outside_vault.write_source(self.source_id, self.revision_id, "outside.txt", b"outside unchanged")
        holder.rename(base / "holder-original")
        holder.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(VaultPathError):
            original.read_bytes(path)
        with self.assertRaises(VaultPathError):
            original.write_source(self.source_id, self.revision_id, "outside.txt", b"must not write")
        self.assertEqual(outside_vault.read_bytes(path), b"outside unchanged")

    def test_write_source_is_immutable_and_returns_relative_path(self) -> None:
        path = self.vault.write_source(
            self.source_id, self.revision_id, "Annual Report.PDF", b"source bytes"
        )

        self.assertEqual(
            path,
            f"Sources/Files/{self.source_id}/{self.revision_id}.pdf",
        )
        self.assertEqual(self.vault.read_bytes(path), b"source bytes")
        self.assertEqual(
            self.vault.write_source(
                self.source_id, self.revision_id, "renamed.pdf", b"source bytes"
            ),
            path,
        )
        with self.assertRaises(VaultConflictError):
            self.vault.write_source(
                self.source_id, self.revision_id, "report.pdf", b"changed bytes"
            )
        self.assertEqual(self.vault.read_bytes(path), b"source bytes")

    def test_resolve_rejects_absolute_and_traversal_paths(self) -> None:
        for path in ("/etc/passwd", "../outside", "Sources/../outside", "C:/outside"):
            with self.subTest(path=path), self.assertRaises(VaultPathError):
                self.vault.resolve(path)

    def test_resolve_and_write_reject_symlink_components(self) -> None:
        self.root.mkdir(parents=True)
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        (self.root / "Sources").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(VaultPathError):
            self.vault.resolve("Sources/Files/sample.txt")
        with self.assertRaises(VaultPathError):
            self.vault.write_source(
                self.source_id, self.revision_id, "sample.txt", b"private"
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_read_rejects_symlink_target(self) -> None:
        self.root.mkdir(parents=True)
        outside = Path(self.temporary_directory.name) / "outside.txt"
        outside.write_bytes(b"outside")
        (self.root / "link.txt").symlink_to(outside)

        with self.assertRaises(VaultPathError):
            self.vault.read_bytes("link.txt")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not supported on this platform")
    def test_read_rejects_fifo_without_waiting_for_a_writer(self) -> None:
        self.root.mkdir(parents=True)
        os.mkfifo(self.root / "unexpected.txt")
        # A subprocess timeout bounds the regression even if blocking open returns.
        result = subprocess.run(
            [sys.executable, "-c", """
import sys
from pathlib import Path
from knowgrain.vault import VaultPathError, VaultStore
try:
    VaultStore(Path(sys.argv[1])).read_bytes('unexpected.txt')
except VaultPathError:
    sys.exit(0)
sys.exit(1)
""", str(self.root)],
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_root_replaced_by_symlink_after_store_creation(self) -> None:
        self.root.mkdir(parents=True)
        outside = Path(self.temporary_directory.name) / "outside-root"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_bytes(b"keep")
        self.root.rmdir()
        self.root.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(VaultPathError):
            self.vault.resolve("keep.txt")
        with self.assertRaises(VaultPathError):
            self.vault.initialize()
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(list(outside.iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
