"""PostgreSQL transaction tests for durable Core cleanup jobs."""

import asyncio
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.models import CoreMaintenanceJob, Job, SourceDocument, SourceRevision
from knowgrain.source_repository import SourceConflictError, SourceRepository
from knowgrain.source_service import SourceService
from knowgrain.vault import VaultStore


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresCoreMaintenanceTests(unittest.IsolatedAsyncioTestCase):
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
        self.vault = VaultStore(self.settings.vault_root)
        self.vault.initialize()
        self.service = SourceService(self.settings, self.repository, self.vault)
        self.source_ids = set()

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
        await self.database.close()

    async def upload(
        self,
        *,
        filename="cleanup.txt",
        content=b"cleanup fixture",
        source_id=None,
    ):
        result = await self.service.import_file(filename, content, source_id=source_id)
        self.source_ids.add(result.source_id)
        return result

    async def indexed_source(self):
        result = await self.upload()
        digest = hashlib.sha256(b"parsed cleanup fixture").hexdigest()
        owner = uuid4()
        self.assertEqual((await self.repository.claim_job(owner))["job_id"], str(result.job_id))
        self.assertTrue(
            await self.repository.complete_job(result.job_id, owner, digest, [{"text": "kept"}])
        )
        return result, digest

    async def delete_source(self, source_id, revision_id):
        return await self.repository.soft_delete_source(
            source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=revision_id,
        )

    async def test_cleanup_manifest_expiry_restore_and_forced_rebuild(self):
        result, parsed_digest = await self.indexed_source()
        original = await self.repository.get_revision(result.revision_id)
        deleted = await self.delete_source(result.source_id, result.revision_id)
        self.assertEqual((deleted["state"], deleted["lifecycle_version"]), ("deleted", 1))
        self.assertEqual(await self.delete_source(result.source_id, result.revision_id), deleted)

        queued = await self.repository.list_maintenance(result.source_id)
        self.assertEqual(len(queued), 1)
        self.assertEqual(
            set(queued[0]),
            {
                "job_id",
                "source_id",
                "revision_id",
                "lifecycle_version",
                "state",
                "attempts",
                "error",
                "created_at",
                "updated_at",
                "lease_until",
            },
        )
        maintenance_id = UUID(queued[0]["job_id"])
        first_owner = uuid4()
        first_claim = await self.repository.claim_maintenance(first_owner)
        self.assertEqual(first_claim["job_id"], str(maintenance_id))
        self.assertIsNone(first_claim["cleanup_chunk_ids"])
        self.assertTrue(
            await self.repository.record_maintenance_chunks(
                maintenance_id, first_owner, ["chunk-a", "chunk-b", "chunk-a"]
            )
        )
        self.assertEqual(
            (await self.repository.list_maintenance(result.source_id))[0]["state"], "running"
        )

        with self.assertRaises(ValueError):
            await self.repository.record_maintenance_chunks(maintenance_id, first_owner, ["x\ny"])
        with self.assertRaises(ValueError):
            await self.repository.record_maintenance_chunks(
                maintenance_id, first_owner, ["x" * 513]
            )
        with self.assertRaises(ValueError):
            await self.repository.record_maintenance_chunks(
                maintenance_id, first_owner, ["x"] * 10_001
            )

        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(CoreMaintenanceJob)
                .where(CoreMaintenanceJob.id == maintenance_id)
                .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
            )
        self.assertFalse(await self.repository.renew_maintenance_lease(maintenance_id, first_owner))
        self.assertFalse(await self.repository.record_maintenance_chunks(maintenance_id, first_owner, []))
        self.assertFalse(await self.repository.complete_maintenance(maintenance_id, first_owner))
        self.assertFalse(await self.repository.fail_maintenance(maintenance_id, first_owner, "late"))

        with self.assertRaises(SourceConflictError):
            await self.repository.restore_source(
                result.source_id,
                expected_lifecycle_version=1,
                expected_latest_revision_id=result.revision_id,
                verified_current_revision_id=result.revision_id,
            )

        second_owner = uuid4()
        second_claim = await self.repository.claim_maintenance(second_owner)
        self.assertEqual(second_claim["job_id"], str(maintenance_id))
        self.assertEqual(second_claim["cleanup_chunk_ids"], ["chunk-a", "chunk-b"])
        self.assertEqual((await self.repository.list_maintenance(result.source_id))[0]["attempts"], 2)
        self.assertTrue(await self.repository.complete_maintenance(maintenance_id, second_owner))

        revision_after_cleanup = await self.repository.get_revision(result.revision_id)
        self.assertEqual(revision_after_cleanup["state"], "failed")
        self.assertEqual(revision_after_cleanup["parsed_text_sha256"], parsed_digest)
        self.assertEqual(revision_after_cleanup["indexed_at"], original["indexed_at"])
        self.assertEqual(revision_after_cleanup["error"], "已清理索引，恢复后将重建")

        restored = await self.repository.restore_source(
            result.source_id,
            expected_lifecycle_version=1,
            expected_latest_revision_id=result.revision_id,
            verified_current_revision_id=result.revision_id,
        )
        self.assertEqual((restored["state"], restored["lifecycle_version"]), ("active", 2))
        self.assertIsNone(restored["current_revision_id"])
        self.assertEqual(restored["latest_revision"]["state"], "queued")

        index_owner = uuid4()
        rebuilt = await self.repository.claim_job(index_owner)
        self.assertEqual(rebuilt["job_id"], str(result.job_id))
        self.assertTrue(rebuilt["force_rebuild"])
        self.assertEqual(rebuilt["cleanup_chunk_ids"], ["chunk-a", "chunk-b"])
        self.assertTrue(
            await self.repository.record_index_cleanup_chunks(
                result.job_id, index_owner, ["deleted-before-rebuild"]
            )
        )
        self.assertTrue(
            await self.repository.complete_job(result.job_id, index_owner, parsed_digest, [])
        )
        async with self.database.session_factory() as session:
            job = await session.get(Job, result.job_id)
            self.assertFalse(job.force_rebuild)
            self.assertEqual(job.cleanup_chunk_ids, ["deleted-before-rebuild"])
        rebuilt_revision = await self.repository.get_revision(result.revision_id)
        self.assertEqual(rebuilt_revision["indexed_at"], original["indexed_at"])
        self.assertEqual(rebuilt_revision["parsed_text_sha256"], parsed_digest)

    async def test_retry_and_release_keep_current_deleted_cycle_only(self):
        result, _parsed_digest = await self.indexed_source()
        await self.delete_source(result.source_id, result.revision_id)
        queued = await self.repository.list_maintenance(result.source_id)
        job_id = UUID(queued[0]["job_id"])
        owner = uuid4()
        await self.repository.claim_maintenance(owner)
        self.assertTrue(await self.repository.fail_maintenance(job_id, owner, "safe failure"))
        retried = await self.repository.retry_maintenance(job_id)
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["attempts"], 1)
        self.assertTrue(await self.repository.retry_maintenance(job_id) == retried)

        stopped_owner = uuid4()
        await self.repository.claim_maintenance(stopped_owner)
        await self.repository.release_maintenance_owner(stopped_owner)
        listed = await self.repository.list_maintenance(result.source_id)
        self.assertEqual(listed[0]["state"], "failed")
        self.assertIsNone(listed[0]["lease_until"])
        self.assertEqual(listed[0]["attempts"], 2)

        self.assertEqual((await self.repository.retry_maintenance(job_id))["state"], "queued")
        restored = await self.repository.restore_source(
            result.source_id,
            expected_lifecycle_version=1,
            expected_latest_revision_id=result.revision_id,
            verified_current_revision_id=result.revision_id,
        )
        self.assertIsNone(restored["current_revision_id"])
        with self.assertRaises(SourceConflictError):
            await self.repository.retry_maintenance(job_id)

    async def test_partial_cleanup_manifest_survives_restore_retry_and_next_delete(self):
        result, _ = await self.indexed_source()
        await self.delete_source(result.source_id, result.revision_id)
        owner = uuid4()
        claim = await self.repository.claim_maintenance(owner)
        cleanup_id = UUID(claim["job_id"])
        self.assertTrue(await self.repository.record_maintenance_chunks(cleanup_id, owner, ["orphan-a"]))
        self.assertTrue(await self.repository.fail_maintenance(cleanup_id, owner, "partial cleanup"))
        await self.repository.restore_source(
            result.source_id, expected_lifecycle_version=1,
            expected_latest_revision_id=result.revision_id,
            verified_current_revision_id=result.revision_id,
        )
        index_owner = uuid4()
        first = await self.repository.claim_job(index_owner)
        self.assertEqual(first["cleanup_chunk_ids"], ["orphan-a"])
        self.assertTrue(await self.repository.record_index_cleanup_chunks(
            result.job_id, index_owner, ["orphan-a", "orphan-b"]
        ))
        self.assertTrue(await self.repository.fail_job(result.job_id, index_owner, "partial rebuild"))
        await self.repository.retry_source(result.source_id)
        second_owner = uuid4()
        second = await self.repository.claim_job(second_owner)
        self.assertEqual(second["cleanup_chunk_ids"], ["orphan-a", "orphan-b"])
        self.assertTrue(await self.repository.fail_job(result.job_id, second_owner, "still partial"))
        await self.repository.soft_delete_source(
            result.source_id, expected_lifecycle_version=2,
            expected_latest_revision_id=result.revision_id,
        )
        next_cleanup = await self.repository.claim_maintenance(uuid4())
        self.assertEqual(next_cleanup["lifecycle_version"], 3)
        self.assertEqual(next_cleanup["cleanup_chunk_ids"], ["orphan-a", "orphan-b"])

    async def test_complete_rejects_lease_expired_while_waiting_for_rows(self):
        result = await self.upload()
        await self.delete_source(result.source_id, result.revision_id)
        owner = uuid4()
        claim = await self.repository.claim_maintenance(owner)
        job_id = UUID(claim["job_id"])

        async with self.database.session_factory() as session, session.begin():
            await session.scalar(
                select(SourceDocument)
                .where(SourceDocument.id == result.source_id)
                .with_for_update()
            )
            await session.scalar(
                select(Job).where(Job.id == result.job_id).with_for_update()
            )
            await session.scalar(
                select(CoreMaintenanceJob)
                .where(CoreMaintenanceJob.id == job_id)
                .with_for_update()
            )
            await session.execute(
                update(CoreMaintenanceJob)
                .where(CoreMaintenanceJob.id == job_id)
                .values(lease_until=func.clock_timestamp() + text("INTERVAL '200 milliseconds'"))
            )
            completion = asyncio.create_task(
                self.repository.complete_maintenance(job_id, owner)
            )
            await asyncio.sleep(0.05)
            self.assertFalse(completion.done(), "completion should wait for the source row lock")
            await asyncio.sleep(0.3)

        self.assertFalse(await completion)
        snapshot = await self.repository.list_maintenance(result.source_id)
        self.assertEqual(snapshot[0]["state"], "running")

    async def test_multiple_sources_and_second_lifecycle_keep_cleanup_scoped(self):
        first_revision = await self.upload(filename="first-a.txt", content=b"source a first")
        latest_revision = await self.upload(
            filename="second-a.txt",
            content=b"source a second",
            source_id=first_revision.source_id,
        )
        other_source = await self.upload(filename="other.txt", content=b"source b")
        source_a = first_revision.source_id
        source_b = other_source.source_id

        await self.repository.soft_delete_source(
            source_a,
            expected_lifecycle_version=0,
            expected_latest_revision_id=latest_revision.revision_id,
        )
        await self.repository.soft_delete_source(
            source_b,
            expected_lifecycle_version=0,
            expected_latest_revision_id=other_source.revision_id,
        )

        initial_claims = []
        first_a_cleanup = None
        first_a_owner = None
        for _ in range(3):
            owner = uuid4()
            claim = await self.repository.claim_maintenance(owner)
            self.assertIsNotNone(claim)
            initial_claims.append(claim)
            if claim["source_id"] == str(source_a) and claim["revision_id"] == str(
                latest_revision.revision_id
            ):
                first_a_cleanup = UUID(claim["job_id"])
                first_a_owner = owner
                self.assertTrue(
                    await self.repository.record_maintenance_chunks(
                        first_a_cleanup, owner, ["source-a-only"]
                    )
                )
            self.assertTrue(
                await self.repository.fail_maintenance(
                    UUID(claim["job_id"]), owner, "fixture cleanup failure"
                )
            )

        self.assertEqual(
            {
                (claim["source_id"], claim["revision_id"], claim["lifecycle_version"])
                for claim in initial_claims
            },
            {
                (str(source_a), str(first_revision.revision_id), 1),
                (str(source_a), str(latest_revision.revision_id), 1),
                (str(source_b), str(other_source.revision_id), 1),
            },
        )
        self.assertIsNotNone(first_a_cleanup)
        self.assertIsNotNone(first_a_owner)

        await self.repository.restore_source(
            source_a,
            expected_lifecycle_version=1,
            expected_latest_revision_id=latest_revision.revision_id,
            verified_current_revision_id=None,
        )
        self.assertFalse(
            await self.repository.record_maintenance_chunks(
                first_a_cleanup, first_a_owner, ["stale-write"]
            )
        )
        self.assertFalse(
            await self.repository.complete_maintenance(first_a_cleanup, first_a_owner)
        )
        with self.assertRaises(SourceConflictError):
            await self.repository.retry_maintenance(first_a_cleanup)

        await self.repository.soft_delete_source(
            source_a,
            expected_lifecycle_version=2,
            expected_latest_revision_id=latest_revision.revision_id,
        )
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(CoreMaintenanceJob)
                .where(CoreMaintenanceJob.id == first_a_cleanup)
                .values(
                    state="running",
                    lease_owner=first_a_owner,
                    lease_until=func.clock_timestamp() + text("INTERVAL '30 seconds'"),
                )
            )
        self.assertFalse(
            await self.repository.record_maintenance_chunks(
                first_a_cleanup, first_a_owner, ["stale-cycle-write"]
            )
        )
        self.assertFalse(
            await self.repository.complete_maintenance(first_a_cleanup, first_a_owner)
        )

        current_a_jobs = await self.repository.list_maintenance(source_a)
        current_a = [job for job in current_a_jobs if job["lifecycle_version"] == 3]
        self.assertEqual(len(current_a), 2)
        async with self.database.session_factory() as session:
            current_a_rows = list(
                (
                    await session.scalars(
                        select(CoreMaintenanceJob).where(
                            CoreMaintenanceJob.source_id == source_a,
                            CoreMaintenanceJob.lifecycle_version == 3,
                        )
                    )
                ).all()
            )
        manifests_by_revision = {
            str(job.revision_id): job.cleanup_chunk_ids for job in current_a_rows
        }
        self.assertEqual(manifests_by_revision[str(first_revision.revision_id)], None)
        self.assertEqual(
            manifests_by_revision[str(latest_revision.revision_id)], ["source-a-only"]
        )
        self.assertTrue(all(job.state == "queued" for job in current_a_rows))

        second_cycle_claims = []
        for _ in range(2):
            claim = await self.repository.claim_maintenance(uuid4())
            self.assertIsNotNone(claim)
            second_cycle_claims.append(claim)
            self.assertEqual(claim["source_id"], str(source_a))
            self.assertEqual(claim["lifecycle_version"], 3)
        self.assertEqual(
            {claim["revision_id"] for claim in second_cycle_claims},
            {str(first_revision.revision_id), str(latest_revision.revision_id)},
        )
        self.assertEqual(
            {
                claim["revision_id"]: claim["cleanup_chunk_ids"]
                for claim in second_cycle_claims
            },
            {
                str(first_revision.revision_id): None,
                str(latest_revision.revision_id): ["source-a-only"],
            },
        )

        source_b_jobs = await self.repository.list_maintenance(source_b)
        self.assertEqual(len(source_b_jobs), 1)
        self.assertEqual(source_b_jobs[0]["lifecycle_version"], 1)
        self.assertEqual(source_b_jobs[0]["state"], "failed")
        self.assertEqual(source_b_jobs[0]["attempts"], 1)
        async with self.database.session_factory() as session:
            source_b_row = await session.scalar(
                select(CoreMaintenanceJob).where(
                    CoreMaintenanceJob.source_id == source_b,
                    CoreMaintenanceJob.lifecycle_version == 1,
                )
            )
        self.assertIsNone(source_b_row.cleanup_chunk_ids)
