from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from knowgrain.config import Settings
from knowgrain.vault import VaultStore
from knowgrain.wiki_files import WikiFileStore
from knowgrain.vault_setup import (
    VAULT_DIRECTORIES,
    VaultPathSetupError,
    VaultSelectionConflict,
    VaultSetupService,
)


class MemoryDatabase:
    """Small database double for filesystem and path-safety behavior."""

    is_ready = True
    last_error = None

    def __init__(self, *, source_count: int = 0, originals=None):
        self.binding = None
        self.source_count = source_count
        self.originals = list(originals or [])

    async def get_vault_state(self):
        return self.binding, self.source_count

    async def list_source_originals(self):
        return list(self.originals)

    async def compare_and_set_vault_binding(
        self, *, expected_binding_id, expected_root, target_root
    ):
        current_id = self.binding["binding_id"] if self.binding else None
        current_root = self.binding["root_path"] if self.binding else expected_root
        if current_id != expected_binding_id or current_root != expected_root:
            from knowgrain.database import VaultBindingConflict

            raise VaultBindingConflict("stale")
        if self.source_count and target_root != expected_root:
            from knowgrain.database import VaultBindingConflict

            raise VaultBindingConflict("locked")
        if self.binding is None:
            self.binding = {
                "binding_id": uuid4(),
                "root_path": target_root,
            }
        elif self.binding["root_path"] != target_root:
            self.binding = {"binding_id": uuid4(), "root_path": target_root}
        return self.binding


class VaultSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.parent = self.base / "allowed"
        self.parent.mkdir()
        self.configured = self.base / "legacy-vault"
        self.database = MemoryDatabase()
        self.settings = Settings(
            _env_file=None,
            vault_root=self.configured,
            vault_parent_dir=self.parent,
        )
        self.service = VaultSetupService(self.settings, self.database)

    async def test_unprojected_wiki_locks_root_before_watcher_updates_database(self):
        vault, binding = await self.service.initialize()
        page = WikiFileStore(vault).create("A page before indexing", "Keep this manual body")
        self.assertEqual(self.database.source_count, 0)
        status = await self.service.status(ready=True, detail=None)
        self.assertFalse(status["selection_enabled"])
        with self.assertRaises(VaultSelectionConflict):
            await self.service.preview("Other")
        with self.assertRaises(VaultSelectionConflict):
            await self.service.validate_selection(
                name="Other", expected_binding_id=binding["binding_id"],
                expected_root=binding["root_path"],
            )
        self.assertEqual(vault.read_bytes(page.vault_path).decode("utf-8"), page.markdown)
        self.assertFalse((self.parent / "Other").exists())

    async def test_preview_is_read_only_and_reports_required_and_missing_folders(self):
        preview = await self.service.preview("Research")

        self.assertFalse(preview["exists"])
        self.assertEqual(preview["directories"], list(VAULT_DIRECTORIES))
        self.assertEqual(preview["create_directories"], list(VAULT_DIRECTORIES))
        self.assertEqual(preview["root"], str(self.parent / "Research"))
        self.assertEqual(preview["expected_root"], str(self.configured))
        self.assertFalse((self.parent / "Research").exists())
        self.assertFalse(self.configured.exists())

    async def test_preview_reports_only_missing_required_directories(self):
        target = self.parent / "Research"
        (target / "Sources" / "Files").mkdir(parents=True)

        preview = await self.service.preview("Research")

        self.assertEqual(preview["directories"], list(VAULT_DIRECTORIES))
        self.assertEqual(
            preview["create_directories"],
            ["Sources/Evidence", "Wiki/Drafts", "Wiki/Pages"],
        )
        self.assertEqual(list((target / "Sources").iterdir()), [target / "Sources" / "Files"])

    async def test_preview_rejects_traversal_drive_syntax_and_symlinks(self):
        for name in ("../escape", "nested/name", "nested\\name", "C:escape", ".", ".."):
            with self.subTest(name=name), self.assertRaises(VaultPathSetupError):
                await self.service.preview(name)

        outside = self.base / "outside"
        outside.mkdir()
        (self.parent / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(VaultPathSetupError):
            await self.service.preview("linked")

        target = self.parent / "UnsafeTree"
        target.mkdir()
        (target / "Sources").mkdir()
        (target / "Sources" / "Files").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(VaultPathSetupError):
            await self.service.preview("UnsafeTree")

    async def test_bound_vault_status_ignores_obsolete_symlinked_initial_suggestion(self):
        bound = self.base / "persisted-vault"
        bound.mkdir()
        linked_suggestion = self.base / "old-vault-alias"
        linked_suggestion.symlink_to(self.base, target_is_directory=True)
        self.settings.vault_root = linked_suggestion
        self.database.binding = {"binding_id": uuid4(), "root_path": str(bound)}
        self.service = VaultSetupService(self.settings, self.database)

        vault, _ = await self.service.initialize()
        status = await self.service.status(ready=True, detail=None)

        self.assertEqual(vault.root, bound)
        self.assertTrue(status["ready"])
        self.assertEqual(status["root"], str(bound))
        self.assertEqual(status["configured_root"], str(linked_suggestion))

    async def test_select_preserves_existing_unrelated_files_and_obsidian(self):
        target = self.parent / "Research"
        target.mkdir()
        obsidian_config = target / ".obsidian" / "app.json"
        obsidian_config.parent.mkdir()
        obsidian_config.write_text('{"keep":true}', encoding="utf-8")
        unrelated = target / "keep.txt"
        unrelated.write_text("user data", encoding="utf-8")

        preview = await self.service.preview("Research")
        candidate, binding, count = await self.service.validate_selection(
            name="Research",
            expected_binding_id=UUID(preview["binding_id"]) if preview["binding_id"] else None,
            expected_root=preview["expected_root"],
        )
        self.assertEqual(count, 0)
        _, persisted = await self.service.commit_selection(
            candidate=candidate,
            expected_binding_id=binding["binding_id"] if binding else None,
            expected_root=preview["expected_root"],
            source_count=count,
        )

        self.assertEqual(persisted["root_path"], str(target))
        self.assertTrue(all((target / relative).is_dir() for relative in VAULT_DIRECTORIES))
        self.assertEqual(obsidian_config.read_text(encoding="utf-8"), '{"keep":true}')
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user data")
        self.assertFalse(any(target.glob(".knowgrain-write-probe-*")))

    async def test_file_at_required_directory_path_is_rejected_without_overwrite(self):
        target = self.parent / "Research"
        target.mkdir()
        collision = target / "Sources" / "Files"
        collision.parent.mkdir()
        collision.write_text("keep me", encoding="utf-8")

        with self.assertRaises(VaultPathSetupError):
            await self.service.preview("Research")
        self.assertEqual(collision.read_text(encoding="utf-8"), "keep me")

    async def test_legacy_originals_are_hashed_before_binding_or_new_folders(self):
        content = b"unchanged legacy source"
        relative = "Sources/Files/source/revision.txt"
        source = self.configured / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        self.database.source_count = 1
        self.database.originals = [(relative, hashlib.sha256(content).hexdigest())]

        vault, binding = await self.service.initialize()

        self.assertEqual(binding["root_path"], str(self.configured))
        self.assertEqual(vault.read_bytes(relative), content)
        self.assertTrue((self.configured / "Wiki/Pages").is_dir())

    async def test_changed_legacy_original_fails_closed_before_directory_creation(self):
        relative = "Sources/Files/source/revision.txt"
        source = self.configured / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"changed bytes")
        self.database.source_count = 1
        self.database.originals = [(relative, hashlib.sha256(b"expected bytes").hexdigest())]

        with self.assertRaises(VaultPathSetupError):
            await self.service.initialize()

        self.assertIsNone(self.database.binding)
        self.assertFalse((self.configured / "Wiki").exists())

    async def test_legacy_fifo_is_rejected_without_blocking(self):
        relative = "Sources/Files/source/pipe.txt"
        fifo = self.configured / relative
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        digest = hashlib.sha256(b"not a FIFO").hexdigest()
        script = """
import sys
from pathlib import Path
from knowgrain.vault_setup import VaultPathSetupError, VaultSetupService
try:
    VaultSetupService._verify_originals_sync(Path(sys.argv[1]), [(sys.argv[2], sys.argv[3])])
except VaultPathSetupError:
    sys.exit(0)
sys.exit(1)
"""

        result = subprocess.run(
            [sys.executable, "-c", script, str(self.configured), relative, digest],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_unbound_legacy_metadata_keeps_selection_locked_when_adoption_fails(self):
        self.database.source_count = 1
        self.database.originals = [
            ("Sources/Files/missing.txt", hashlib.sha256(b"missing").hexdigest())
        ]

        status = await self.service.status(ready=False, detail="legacy source missing")

        self.assertFalse(status["selection_enabled"])
        self.assertEqual(status["binding_id"], None)

    async def test_bound_root_with_replaced_original_fails_closed(self):
        relative = "Sources/Files/source/revision.txt"
        source = self.configured / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"changed bytes")
        self.database.source_count = 1
        self.database.originals = [(relative, hashlib.sha256(b"expected bytes").hexdigest())]
        self.database.binding = {
            "binding_id": uuid4(),
            "root_path": str(self.configured),
        }

        with self.assertRaises(VaultPathSetupError):
            await self.service.initialize()

        self.assertEqual(self.database.binding["root_path"], str(self.configured))
        self.assertFalse((self.configured / "Wiki").exists())

    async def test_same_root_selection_revalidates_registered_originals(self):
        self.configured = self.parent / "Research"
        self.settings.vault_root = self.configured
        self.service = VaultSetupService(self.settings, self.database)
        relative = "Sources/Files/source/revision.txt"
        source = self.configured / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"changed bytes")
        self.database.source_count = 1
        self.database.originals = [(relative, hashlib.sha256(b"expected bytes").hexdigest())]
        self.database.binding = {
            "binding_id": uuid4(),
            "root_path": str(self.configured),
        }

        candidate, binding, count = await self.service.validate_selection(
            name=self.configured.name,
            expected_binding_id=self.database.binding["binding_id"],
            expected_root=str(self.configured),
        )
        with self.assertRaises(VaultPathSetupError):
            await self.service.commit_selection(
                candidate=candidate,
                expected_binding_id=binding["binding_id"],
                expected_root=str(self.configured),
                source_count=count,
            )

    async def test_populated_binding_rejects_different_root_and_stale_cas(self):
        target = self.parent / "Research"
        target.mkdir()
        self.database.binding = {"binding_id": uuid4(), "root_path": str(self.configured)}
        self.database.source_count = 1
        with self.assertRaises(VaultSelectionConflict):
            await self.service.preview("Research")

        with self.assertRaises(VaultSelectionConflict):
            await self.service.validate_selection(
                name="Research",
                expected_binding_id=self.database.binding["binding_id"],
                expected_root=self.database.binding["root_path"],
            )
        with self.assertRaises(VaultSelectionConflict):
            await self.service.validate_selection(
                name="Research",
                expected_binding_id=uuid4(),
                expected_root=self.database.binding["root_path"],
            )
