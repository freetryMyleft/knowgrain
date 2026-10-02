"""PostgreSQL transaction tests for source file-operation journals."""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.models import Job, SourceDocument, SourceFileOperation, SourceRevision, VaultBinding
from knowgrain.source_file_repository import SourceFileRepository
from knowgrain.source_repository import SourceConflictError, SourceRepository
from knowgrain.source_service import SourceService
from knowgrain.vault import VaultStore


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresSourceFileRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            postgres_database=TEST_DATABASE,
            knowgrain_postgres_db=TEST_DATABASE,
            vault_root=Path(self.temporary.name) / "vault",
            vault_parent_dir=Path(self.temporary.name) / "vaults",
            llm_model="knowgrain-test-missing-model",
            embedding_model="knowgrain-test-missing-embedding",
        )
        self.database = ApplicationDatabase(self.settings)
        self.assertTrue(await self.database.initialize(), self.database.last_error)
        binding, source_count = await self.database.get_vault_state()
        if binding is not None or source_count:
            await self.database.close()
            self.skipTest("fixture database must start without source rows or a Vault binding")
        self.repository = SourceRepository(self.database)
        self.file_repository = SourceFileRepository(self.database, self.repository)
        self.vault = VaultStore(self.settings.vault_root)
        self.vault.initialize()
        self.service = SourceService(self.settings, self.repository, self.vault)
        self.source_ids = set()
        self.fixture_binding_id = None

    async def asyncTearDown(self):
        async with self.database.session_factory() as session, session.begin():
            ids = list(self.source_ids)
            revisions = select(SourceRevision.id).where(SourceRevision.source_id.in_(ids))
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id.in_(ids))
                .values(latest_revision_id=None, current_revision_id=None)
            )
            await session.execute(delete(Job).where(Job.revision_id.in_(revisions)))
            await session.execute(delete(SourceRevision).where(SourceRevision.source_id.in_(ids)))
            await session.execute(delete(SourceDocument).where(SourceDocument.id.in_(ids)))
            if self.fixture_binding_id is not None:
                await session.execute(
                    delete(VaultBinding).where(
                        VaultBinding.id == 1,
                        VaultBinding.binding_id == self.fixture_binding_id,
                    )
                )
        await self.database.close()

    async def upload(self, filename: str, content: bytes, source_id=None):
        result = await self.service.import_file(filename, content, source_id=source_id)
        self.source_ids.add(result.source_id)
        return result

    async def finish_one_cleanup(self, source_id):
        owner = uuid4()
        claim = await self.repository.claim_maintenance(owner)
        self.assertEqual(claim["source_id"], str(source_id))
        self.assertTrue(await self.repository.complete_maintenance(UUID(claim["job_id"]), owner))
        return claim

    async def test_cleaned_archive_journal_restore_fence_and_next_lifecycle(self):
        first = await self.upload("first.txt", b"first original")
        latest = await self.upload("latest.md", b"latest original", source_id=first.source_id)
        await self.repository.soft_delete_source(
            first.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=latest.revision_id,
        )

        await self.finish_one_cleanup(first.source_id)
        self.assertEqual(await self.file_repository.list_file_operations(first.source_id), [])
        self.assertIsNone(await self.file_repository.enqueue_archive_if_cleaned(first.source_id))
        await self.finish_one_cleanup(first.source_id)

        archive_operations = await self.file_repository.list_file_operations(first.source_id)
        self.assertEqual(len(archive_operations), 1)
        archive = archive_operations[0]
        self.assertEqual(
            set(archive),
            {
                "operation_id",
                "source_id",
                "lifecycle_version",
                "kind",
                "state",
                "attempts",
                "error",
                "created_at",
                "updated_at",
                "lease_until",
            },
        )
        self.assertEqual(
            (archive["kind"], archive["state"], archive["attempts"]),
            ("archive", "queued", 0),
        )

        archive_owner = uuid4()
        first_claim = await self.file_repository.claim_file_operation(
            archive_owner, operation_id=UUID(archive["operation_id"])
        )
        self.assertEqual(first_claim["kind"], "archive")
        self.assertEqual(
            {entry["revision_id"] for entry in first_claim["manifest"]},
            {str(first.revision_id), str(latest.revision_id)},
        )
        self.assertNotIn("lease_owner", first_claim)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceFileOperation)
                .where(SourceFileOperation.id == UUID(archive["operation_id"]))
                .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
            )
        self.assertFalse(
            await self.file_repository.renew_file_lease(
                UUID(archive["operation_id"]), archive_owner
            )
        )
        self.assertIsNone(
            await self.file_repository.complete_file_operation(
                UUID(archive["operation_id"]), archive_owner
            )
        )

        replacement_owner = uuid4()
        replacement_claim = await self.file_repository.claim_file_operation(
            replacement_owner, operation_id=UUID(archive["operation_id"])
        )
        self.assertEqual(replacement_claim["operation_id"], archive["operation_id"])
        self.assertEqual(
            (await self.file_repository.list_file_operations(first.source_id))[0]["attempts"], 2
        )
        completed_archive = await self.file_repository.complete_file_operation(
            UUID(archive["operation_id"]), replacement_owner
        )
        self.assertEqual(
            (completed_archive["kind"], completed_archive["state"]),
            ("archive", "succeeded"),
        )

        with self.assertRaises(SourceConflictError):
            await self.repository.restore_source(
                first.source_id,
                expected_lifecycle_version=1,
                expected_latest_revision_id=latest.revision_id,
                verified_current_revision_id=latest.revision_id,
            )

        prepared = await self.file_repository.prepare_restore(
            first.source_id,
            expected_lifecycle_version=1,
            expected_latest_revision_id=latest.revision_id,
        )
        self.assertEqual(prepared["state"], "queued")
        restore_owner = uuid4()
        restore_claim = await self.file_repository.claim_file_operation(
            restore_owner, operation_id=UUID(prepared["operation_id"])
        )
        restored = await self.file_repository.complete_file_operation(
            UUID(restore_claim["operation_id"]), restore_owner
        )
        self.assertEqual((restored["state"], restored["lifecycle_version"]), ("active", 2))
        self.assertIsNone(restored["current_revision_id"])
        self.assertEqual(restored["latest_revision"]["state"], "queued")
        self.assertTrue(
            await self.file_repository.prepare_restore(
                first.source_id,
                expected_lifecycle_version=1,
                expected_latest_revision_id=latest.revision_id,
            )
            == {
                "already_restored": True,
                "source": restored,
                "operation_id": prepared["operation_id"],
            }
        )

        deleted_again = await self.repository.soft_delete_source(
            first.source_id,
            expected_lifecycle_version=2,
            expected_latest_revision_id=latest.revision_id,
        )
        self.assertEqual(deleted_again["lifecycle_version"], 3)
        await self.finish_one_cleanup(first.source_id)
        await self.finish_one_cleanup(first.source_id)
        operations = await self.file_repository.list_file_operations(first.source_id)
        self.assertEqual(
            [(item["lifecycle_version"], item["kind"], item["state"]) for item in operations],
            [(3, "archive", "queued"), (1, "restore", "succeeded"), (1, "archive", "succeeded")],
        )

    async def test_restore_intent_cancels_unstarted_cleanup_and_blocks_retries(self):
        result = await self.upload("queued.txt", b"queued original")
        await self.repository.soft_delete_source(
            result.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=result.revision_id,
        )
        maintenance = (await self.repository.list_maintenance(result.source_id))[0]
        prepared = await self.file_repository.prepare_restore(
            result.source_id,
            expected_lifecycle_version=1,
            expected_latest_revision_id=result.revision_id,
        )
        self.assertEqual(prepared["state"], "queued")
        with self.assertRaises(SourceConflictError):
            await self.repository.retry_maintenance(UUID(maintenance["job_id"]))
        self.assertIsNone(await self.repository.claim_maintenance(uuid4()))

        owner = uuid4()
        claim = await self.file_repository.claim_file_operation(
            owner, operation_id=UUID(prepared["operation_id"])
        )
        restored = await self.file_repository.complete_file_operation(
            UUID(claim["operation_id"]), owner
        )
        self.assertEqual((restored["state"], restored["lifecycle_version"]), ("active", 2))
        self.assertIsNone(restored["current_revision_id"])

    async def test_bounded_cleanup_scan_skips_ineligible_older_sources(self):
        queued = await self.upload("queued.txt", b"already queued")
        await self.repository.soft_delete_source(
            queued.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=queued.revision_id,
        )
        await self.finish_one_cleanup(queued.source_id)
        self.assertEqual(
            (await self.file_repository.list_file_operations(queued.source_id))[0]["state"],
            "queued",
        )

        uncleaned = await self.upload("uncleaned.txt", b"not cleaned yet")
        await self.repository.soft_delete_source(
            uncleaned.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=uncleaned.revision_id,
        )
        failed_owner = uuid4()
        uncleaned_claim = await self.repository.claim_maintenance(failed_owner)
        self.assertEqual(uncleaned_claim["source_id"], str(uncleaned.source_id))
        self.assertTrue(
            await self.repository.fail_maintenance(
                UUID(uncleaned_claim["job_id"]), failed_owner, "fixture cleanup failure"
            )
        )

        orphan = await self.upload("orphan.txt", b"cleanup committed without journal")
        await self.repository.soft_delete_source(
            orphan.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=orphan.revision_id,
        )
        await self.finish_one_cleanup(orphan.source_id)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                delete(SourceFileOperation).where(
                    SourceFileOperation.source_id == orphan.source_id,
                    SourceFileOperation.lifecycle_version == 1,
                    SourceFileOperation.kind == "archive",
                )
            )

        self.assertEqual(await self.file_repository.enqueue_cleaned_sources(limit=2), 1)
        orphan_operations = await self.file_repository.list_file_operations(orphan.source_id)
        self.assertEqual(
            [(operation["kind"], operation["state"]) for operation in orphan_operations],
            [("archive", "queued")],
        )

    async def test_completion_checks_lease_after_waiting_for_source_lock(self):
        result = await self.upload("lease.txt", b"lease owner fixture")
        await self.repository.soft_delete_source(
            result.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=result.revision_id,
        )
        await self.finish_one_cleanup(result.source_id)
        operation = (await self.file_repository.list_file_operations(result.source_id))[0]
        owner = uuid4()
        claim = await self.file_repository.claim_file_operation(
            owner, operation_id=UUID(operation["operation_id"])
        )
        self.assertIsNotNone(claim)

        async with self.database.session_factory() as lock_session:
            async with lock_session.begin():
                await lock_session.scalar(
                    select(SourceDocument)
                    .where(SourceDocument.id == result.source_id)
                    .with_for_update()
                )
                await lock_session.execute(
                    update(SourceFileOperation)
                    .where(SourceFileOperation.id == UUID(operation["operation_id"]))
                    .values(
                        lease_until=func.clock_timestamp() + text("INTERVAL '200 milliseconds'")
                    )
                )
                completion = asyncio.create_task(
                    self.file_repository.complete_file_operation(
                        UUID(operation["operation_id"]), owner
                    )
                )
                await asyncio.sleep(0.05)
                self.assertFalse(completion.done())
                await asyncio.sleep(0.25)

        self.assertIsNone(await completion)
        source = await self.repository.get_source(result.source_id)
        self.assertEqual((source["state"], source["lifecycle_version"]), ("deleted", 1))
        replacement_owner = uuid4()
        replacement_claim = await self.file_repository.claim_file_operation(
            replacement_owner, operation_id=UUID(operation["operation_id"])
        )
        self.assertEqual(
            (await self.file_repository.list_file_operations(result.source_id))[0]["attempts"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
