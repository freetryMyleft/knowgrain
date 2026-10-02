"""Real PostgreSQL transaction tests for startup reconciliation fencing."""

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.models import (
    CoreMaintenanceJob,
    Job,
    SourceDocument,
    SourceFileOperation,
    SourceRevision,
)
from knowgrain.source_repository import SourceRepository
from knowgrain.source_service import SourceService
from knowgrain.vault import VaultStore


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


class ReconciliationInputValidationTests(unittest.TestCase):
    @staticmethod
    def candidate():
        return {
            "source_id": str(UUID(int=1)),
            "revision_id": str(UUID(int=2)),
            "job_id": str(UUID(int=3)),
            "lifecycle_version": 0,
            "vault_path": "sources/file.md",
            "filename": "file.md",
            "sha256": "a" * 64,
            "parsed_text_sha256": "b" * 64,
            "indexed_at": datetime(2020, 1, 1, tzinfo=UTC),
            "job_updated_at": datetime(2020, 1, 2, tzinfo=UTC),
            "cleanup_chunk_ids": ["chunk-a", "chunk-a"],
        }

    def test_candidate_validation_canonicalizes_cleanup_manifest(self):
        normalized = SourceRepository._validate_reconciliation_candidate(self.candidate())
        self.assertEqual(normalized["source_id"], UUID(int=1))
        self.assertEqual(normalized["cleanup_chunk_ids"], ("chunk-a",))

    def test_candidate_validation_rejects_invalid_snapshot_shapes(self):
        for field, value in (
            ("source_id", "ABCDEFAB-CDEF-ABCD-EFAB-CDEFABCDEFAB"),
            ("lifecycle_version", True),
            ("sha256", "A" * 64),
            ("parsed_text_sha256", "x" * 65),
            ("parsed_text_sha256", 12),
            ("indexed_at", datetime(2020, 1, 1)),
            ("job_updated_at", "2020-01-02T00:00:00Z"),
            ("cleanup_chunk_ids", "chunk-a"),
        ):
            with self.subTest(field=field):
                candidate = self.candidate()
                candidate[field] = value
                with self.assertRaises(ValueError):
                    SourceRepository._validate_reconciliation_candidate(candidate)

    def test_invalid_metadata_fields_are_preserved_in_validated_snapshot(self):
        candidate = self.candidate()
        candidate["parsed_text_sha256"] = None
        candidate["indexed_at"] = None
        normalized = SourceRepository._validate_reconciliation_candidate(candidate)
        self.assertIsNone(normalized["parsed_text_sha256"])
        self.assertIsNone(normalized["indexed_at"])


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresReconciliationTests(unittest.IsolatedAsyncioTestCase):
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
        self.source_ids: set[UUID] = set()

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

    async def upload(self, filename: str = "file.md", content: bytes | None = None, source_id=None):
        content = content or f"fixture {uuid4()}".encode()
        result = await self.service.import_file(filename, content, source_id=source_id)
        self.source_ids.add(result.source_id)
        return result

    async def mark_indexed(self, result, *, prior_updated_at: datetime | None = None):
        indexed_at = datetime.now(UTC)
        parsed_hash = hashlib.sha256(f"parsed {result.revision_id}".encode()).hexdigest()
        old_updated_at = prior_updated_at or datetime(2020, 1, 1, tzinfo=UTC)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == result.revision_id)
                .values(
                    index_state="ready",
                    parsed_text_sha256=parsed_hash,
                    indexed_at=indexed_at,
                    error="old revision error",
                )
            )
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id == result.source_id)
                .values(current_revision_id=result.revision_id)
            )
            await session.execute(
                update(Job)
                .where(Job.id == result.job_id)
                .values(
                    state="succeeded",
                    updated_at=old_updated_at,
                    error="old job error",
                    lease_owner=uuid4(),
                    lease_until=datetime(2099, 1, 1, tzinfo=UTC),
                )
            )
        return indexed_at, parsed_hash

    async def candidate_for(self, result):
        candidates = await self.repository.list_reconciliation_candidates()
        return next(row for row in candidates if row["source_id"] == str(result.source_id))

    async def test_candidates_are_healthy_and_keyset_paginated(self):
        first = await self.upload("first.md")
        second = await self.upload("second.md")
        latest = await self.upload("latest.md", source_id=first.source_id)
        for result in (first, second, latest):
            await self.mark_indexed(result)

        candidates = await self.repository.list_reconciliation_candidates(limit=1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_id"], str(min(first.source_id, second.source_id)))
        self.assertEqual(candidates[0]["cleanup_chunk_ids"], [])
        self.assertEqual(
            set(candidates[0]),
            {
                "source_id",
                "revision_id",
                "job_id",
                "lifecycle_version",
                "vault_path",
                "filename",
                "sha256",
                "parsed_text_sha256",
                "indexed_at",
                "job_updated_at",
                "cleanup_chunk_ids",
            },
        )
        next_page = await self.repository.list_reconciliation_candidates(
            after_source_id=UUID(candidates[0]["source_id"]), limit=10
        )
        self.assertEqual(
            [row["source_id"] for row in next_page],
            [str(source_id) for source_id in sorted({first.source_id, second.source_id})[1:]],
        )
        self.assertEqual(
            await self.repository.list_reconciliation_candidates(after_source_id=max(first.source_id, second.source_id)),
            [],
        )

    async def test_repair_fences_and_merges_durable_cleanup_manifest(self):
        result = await self.upload()
        indexed_at, parsed_hash = await self.mark_indexed(result)
        async with self.database.session_factory() as session, session.begin():
            session.add(
                CoreMaintenanceJob(
                    source_id=result.source_id,
                    revision_id=result.revision_id,
                    lifecycle_version=0,
                    cleanup_chunk_ids=["maintenance-chunk"],
                )
            )
            await session.execute(
                update(Job)
                .where(Job.id == result.job_id)
                .values(cleanup_chunk_ids=["index-chunk"])
            )

        candidate = await self.candidate_for(result)

        queued_job_id = await self.repository.queue_reconciliation_repair(
            candidate,
            chunk_ids=["provided-chunk", "index-chunk"],
            reason="missing",
        )
        self.assertEqual(queued_job_id, result.job_id)
        async with self.database.session_factory() as session:
            source = await session.get(SourceDocument, result.source_id)
            revision = await session.get(SourceRevision, result.revision_id)
            job = await session.get(Job, result.job_id)
            self.assertEqual(source.lifecycle_version, 0)
            self.assertIsNone(source.current_revision_id)
            self.assertEqual(revision.index_state, "queued")
            self.assertIsNone(revision.error)
            self.assertEqual(revision.indexed_at, indexed_at)
            self.assertEqual(revision.parsed_text_sha256, parsed_hash)
            self.assertEqual(job.state, "queued")
            self.assertTrue(job.force_rebuild)
            self.assertEqual(
                job.cleanup_chunk_ids,
                ["index-chunk", "maintenance-chunk", "provided-chunk"],
            )
            self.assertIsNone(job.lease_owner)
            self.assertIsNone(job.lease_until)
            self.assertIsNone(job.error)
            self.assertGreater(job.updated_at, candidate["job_updated_at"])

        self.assertIsNone(
            await self.repository.queue_reconciliation_repair(
                candidate, chunk_ids=["provided-chunk"], reason="missing"
            )
        )

    async def test_invalid_metadata_repair_preserves_observed_values(self):
        missing_hash = await self.upload("missing-hash.md")
        original_indexed_at, _ = await self.mark_indexed(missing_hash)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == missing_hash.revision_id)
                .values(parsed_text_sha256=None)
            )
        missing_hash_candidate = await self.candidate_for(missing_hash)
        self.assertIsNone(missing_hash_candidate["parsed_text_sha256"])
        self.assertEqual(
            await self.repository.queue_reconciliation_repair(
                missing_hash_candidate, reason="invalid_metadata"
            ),
            missing_hash.job_id,
        )
        async with self.database.session_factory() as session:
            revision = await session.get(SourceRevision, missing_hash.revision_id)
            self.assertIsNone(revision.parsed_text_sha256)
            self.assertEqual(revision.indexed_at, original_indexed_at)

        missing_index_time = await self.upload("missing-index-time.md")
        _, original_hash = await self.mark_indexed(missing_index_time)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == missing_index_time.revision_id)
                .values(parsed_text_sha256="malformed", indexed_at=None)
            )
        missing_index_candidate = await self.candidate_for(missing_index_time)
        self.assertEqual(missing_index_candidate["parsed_text_sha256"], "malformed")
        self.assertIsNone(missing_index_candidate["indexed_at"])
        self.assertEqual(
            await self.repository.queue_reconciliation_repair(
                missing_index_candidate, reason="invalid_metadata"
            ),
            missing_index_time.job_id,
        )
        async with self.database.session_factory() as session:
            revision = await session.get(SourceRevision, missing_index_time.revision_id)
            self.assertEqual(revision.parsed_text_sha256, "malformed")
            self.assertIsNone(revision.indexed_at)
            self.assertNotEqual(revision.parsed_text_sha256, original_hash)

    async def test_import_delete_retry_and_updated_snapshot_all_stale(self):
        imported = await self.upload("import-before.md")
        await self.mark_indexed(imported)
        import_candidate = await self.candidate_for(imported)
        await self.upload("import-after.md", source_id=imported.source_id)
        self.assertIsNone(
            await self.repository.queue_reconciliation_repair(
                import_candidate, reason="inconsistent"
            )
        )

        deleted = await self.upload("delete-before.md")
        await self.mark_indexed(deleted)
        delete_candidate = await self.candidate_for(deleted)
        await self.repository.soft_delete_source(
            deleted.source_id,
            expected_lifecycle_version=0,
            expected_latest_revision_id=deleted.revision_id,
        )
        self.assertIsNone(
            await self.repository.queue_reconciliation_repair(delete_candidate, reason="missing")
        )

        retried = await self.upload("retry.md")
        await self.mark_indexed(retried)
        retry_candidate = await self.candidate_for(retried)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == retried.revision_id)
                .values(index_state="queued")
            )
            await session.execute(
                update(Job)
                .where(Job.id == retried.job_id)
                .values(state="queued", updated_at=datetime.now(UTC) + timedelta(seconds=10))
            )
        self.assertIsNone(
            await self.repository.queue_reconciliation_repair(retry_candidate, reason="missing")
        )

        updated = await self.upload("updated-at.md")
        await self.mark_indexed(updated)
        updated_candidate = await self.candidate_for(updated)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == updated.job_id)
                .values(updated_at=datetime.now(UTC) + timedelta(seconds=10))
            )
        self.assertIsNone(
            await self.repository.queue_reconciliation_repair(updated_candidate, reason="missing")
        )

    async def test_pending_file_operation_excludes_and_fences_candidate(self):
        result = await self.upload()
        await self.mark_indexed(result)
        candidate = await self.candidate_for(result)
        async with self.database.session_factory() as session, session.begin():
            session.add(
                SourceFileOperation(
                    source_id=result.source_id,
                    lifecycle_version=0,
                    kind="archive",
                    manifest=[],
                    expected_latest_revision_id=result.revision_id,
                )
            )

        self.assertEqual(await self.repository.list_reconciliation_candidates(), [])
        self.assertIsNone(
            await self.repository.queue_reconciliation_repair(candidate, reason="missing")
        )

    async def test_vault_file_expectations_use_current_lifecycle_only(self):
        active = await self.upload("active.md")
        deleted_without_journal = await self.upload("deleted-no-journal.md")
        queued_archive = await self.upload("queued-archive.md")
        succeeded_archive = await self.upload("succeeded-archive.md")
        current_restore = await self.upload("current-restore.md")
        prior_cycle = await self.upload("prior-cycle.md")

        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id == deleted_without_journal.source_id)
                .values(state="deleted", lifecycle_version=1)
            )
            await session.execute(
                update(SourceDocument)
                .where(
                    SourceDocument.id.in_(
                        [
                            queued_archive.source_id,
                            succeeded_archive.source_id,
                            current_restore.source_id,
                            prior_cycle.source_id,
                        ]
                    )
                )
                .values(state="deleted", lifecycle_version=2)
            )
            session.add_all(
                [
                    SourceFileOperation(
                        source_id=queued_archive.source_id,
                        lifecycle_version=2,
                        kind="archive",
                        state="queued",
                        manifest=[],
                        expected_latest_revision_id=queued_archive.revision_id,
                    ),
                    SourceFileOperation(
                        source_id=succeeded_archive.source_id,
                        lifecycle_version=2,
                        kind="archive",
                        state="succeeded",
                        manifest=[],
                        expected_latest_revision_id=succeeded_archive.revision_id,
                    ),
                    SourceFileOperation(
                        source_id=current_restore.source_id,
                        lifecycle_version=2,
                        kind="restore",
                        state="succeeded",
                        manifest=[],
                        expected_latest_revision_id=current_restore.revision_id,
                    ),
                    SourceFileOperation(
                        source_id=prior_cycle.source_id,
                        lifecycle_version=1,
                        kind="archive",
                        state="succeeded",
                        manifest=[],
                        expected_latest_revision_id=prior_cycle.revision_id,
                    ),
                ]
            )

        expectations = await self.database.list_source_file_expectations()
        by_source = {str(row["source_id"]): row for row in expectations}
        self.assertEqual(by_source[str(active.source_id)]["allowed_locations"], ("vault",))
        self.assertEqual(
            by_source[str(deleted_without_journal.source_id)]["allowed_locations"],
            ("vault",),
        )
        self.assertEqual(
            by_source[str(queued_archive.source_id)]["allowed_locations"],
            ("vault", "trash"),
        )
        self.assertEqual(
            by_source[str(succeeded_archive.source_id)]["allowed_locations"], ("trash",)
        )
        self.assertEqual(
            by_source[str(current_restore.source_id)]["allowed_locations"],
            ("vault", "trash"),
        )
        self.assertEqual(
            by_source[str(prior_cycle.source_id)]["allowed_locations"], ("vault",)
        )


if __name__ == "__main__":
    unittest.main()
