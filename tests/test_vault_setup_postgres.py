"""Vault binding transactions against an explicitly selected disposable database."""

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.models import Job, SourceDocument, SourceRevision, VaultBinding
from knowgrain.source_repository import SourceRepository
from knowgrain.source_service import SourceService
from knowgrain.vault import VaultStore
from knowgrain.vault_setup import (
    VaultPathSetupError,
    VaultSelectionConflict,
    VaultSetupService,
)


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresVaultSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.parent = self.base / "vaults"
        self.parent.mkdir()
        self.settings = self._settings(self.base / "initial-vault")
        self.database = ApplicationDatabase(self.settings)
        self.assertTrue(await self.database.initialize(), self.database.last_error)
        binding, source_count = await self.database.get_vault_state()
        if binding is not None or source_count:
            await self.database.close()
            self.skipTest("fixture database must start without source rows or a Vault binding")
        self.repository = SourceRepository(self.database)
        self.service = VaultSetupService(self.settings, self.database)
        self.source_ids: set[UUID] = set()
        self.owned_binding_id: UUID | None = None

    def _settings(self, root: Path) -> Settings:
        return Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            postgres_password="knowgrain-local",
            postgres_database=TEST_DATABASE,
            knowgrain_postgres_db=TEST_DATABASE,
            vault_root=root,
            vault_parent_dir=self.parent,
        )

    async def asyncTearDown(self):
        if not getattr(self, "database", None):
            return
        async with self.database.session_factory() as session, session.begin():
            ids = list(self.source_ids)
            if ids:
                revisions = select(SourceRevision.id).where(SourceRevision.source_id.in_(ids))
                await session.execute(
                    update(SourceDocument)
                    .where(SourceDocument.id.in_(ids))
                    .values(latest_revision_id=None, current_revision_id=None)
                )
                await session.execute(delete(Job).where(Job.revision_id.in_(revisions)))
                await session.execute(delete(SourceRevision).where(SourceRevision.source_id.in_(ids)))
                await session.execute(delete(SourceDocument).where(SourceDocument.id.in_(ids)))
            if self.owned_binding_id is not None:
                await session.execute(
                    delete(VaultBinding).where(
                        VaultBinding.id == 1,
                        VaultBinding.binding_id == self.owned_binding_id,
                    )
                )
        await self.database.close()

    async def test_selection_persists_across_restart_and_locks_after_source_import(self):
        _, initial_binding = await self.service.initialize()
        self.owned_binding_id = initial_binding["binding_id"]
        preview = await self.service.preview("Research")
        self.assertFalse(preview["exists"])
        self.assertFalse((self.parent / "Research").exists())

        candidate, _, source_count = await self.service.validate_selection(
            name="Research",
            expected_binding_id=initial_binding["binding_id"],
            expected_root=initial_binding["root_path"],
        )
        self.assertEqual(source_count, 0)
        selected_vault, selected_binding = await self.service.commit_selection(
            candidate=candidate,
            expected_binding_id=initial_binding["binding_id"],
            expected_root=initial_binding["root_path"],
            source_count=source_count,
        )
        self.owned_binding_id = selected_binding["binding_id"]
        self.assertEqual(selected_binding["root_path"], str(self.parent.resolve() / "Research"))
        self.assertNotEqual(selected_binding["binding_id"], initial_binding["binding_id"])

        await self.database.close()
        restarted_settings = self._settings(self.base / "changed-config-suggestion")
        restarted_database = ApplicationDatabase(restarted_settings)
        self.database = restarted_database
        self.assertTrue(await restarted_database.initialize(), restarted_database.last_error)
        restarted_service = VaultSetupService(restarted_settings, restarted_database)
        restarted_vault, persisted = await restarted_service.initialize()
        self.assertEqual(persisted["binding_id"], selected_binding["binding_id"])
        self.assertEqual(restarted_vault.root, selected_vault.root)
        self.assertFalse((self.base / "changed-config-suggestion").exists())

        source_service = SourceService(
            restarted_settings, SourceRepository(restarted_database), restarted_vault
        )
        imported = await source_service.import_file("evidence.txt", b"persisted source")
        self.source_ids.add(imported.source_id)
        with self.assertRaises(VaultSelectionConflict):
            await restarted_service.preview("Other")
        with self.assertRaises(VaultSelectionConflict):
            await restarted_service.validate_selection(
                name="Other",
                expected_binding_id=selected_binding["binding_id"],
                expected_root=selected_binding["root_path"],
            )
        with self.assertRaises(VaultSelectionConflict):
            await restarted_service.validate_selection(
                name="Other",
                expected_binding_id=initial_binding["binding_id"],
                expected_root=initial_binding["root_path"],
            )

    async def test_legacy_original_is_adopted_only_when_hash_matches(self):
        source_id = uuid4()
        revision_id = uuid4()
        content = b"legacy original content"
        relative_path = f"Sources/Files/{source_id}/{revision_id}.txt"
        original = self.settings.vault_root / relative_path
        original.parent.mkdir(parents=True)
        original.write_bytes(content)
        async with self.database.session_factory() as session, session.begin():
            session.add(SourceDocument(id=source_id, filename="legacy.txt", state="active"))
            session.add(
                SourceRevision(
                    id=revision_id,
                    source_id=source_id,
                    filename="legacy.txt",
                    sha256=hashlib.sha256(content).hexdigest(),
                    vault_path=relative_path,
                    media_type="text/plain",
                    index_state="queued",
                )
            )
        self.source_ids.add(source_id)

        vault, binding = await self.service.initialize()

        self.owned_binding_id = binding["binding_id"]
        self.assertEqual(binding["root_path"], str(self.service.configured_root()))
        self.assertEqual(vault.read_bytes(relative_path), content)
        self.assertTrue((self.settings.vault_root / "Wiki/Pages").is_dir())

    async def test_bound_missing_or_changed_original_keeps_database_binding_and_fails_closed(self):
        vault, binding = await self.service.initialize()
        self.owned_binding_id = binding["binding_id"]
        source_service = SourceService(self.settings, self.repository, vault)
        imported = await source_service.import_file("evidence.txt", b"registered bytes")
        self.source_ids.add(imported.source_id)
        original = vault.resolve(imported.vault_path)
        original.write_bytes(b"changed bytes")

        with self.assertRaises(VaultPathSetupError):
            await self.service.initialize()

        stored, count = await self.database.get_vault_state()
        self.assertEqual(count, 1)
        self.assertEqual(stored["binding_id"], binding["binding_id"])
        self.assertEqual(stored["root_path"], str(vault.root))
