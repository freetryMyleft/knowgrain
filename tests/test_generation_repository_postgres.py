"""PostgreSQL transaction checks for durable M3 generation records.

These tests require the explicitly selected disposable ``knowgrain_test`` database.
Fixture cleanup deletes only UUIDs created by this test instance.
"""

import asyncio
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.generation_repository import GenerationConflictError, GenerationRepository
from knowgrain.m3_types import Evidence, EvidenceUnavailableError, evidence_identity
from knowgrain.models import (
    EvidenceRef,
    GeneratedPage,
    GenerationJob,
    PageEvidence,
    SourceDocument,
    SourceRevision,
    WikiPage,
)


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresGenerationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            postgres_password="knowgrain-local",
            postgres_database=TEST_DATABASE,
            knowgrain_postgres_db=TEST_DATABASE,
            vault_root=Path(self.temporary.name) / "vault",
            vault_parent_dir=Path(self.temporary.name) / "vaults",
        )
        self.database = ApplicationDatabase(self.settings)
        self.assertTrue(await self.database.initialize(), self.database.last_error)
        async with self.database.session_factory() as session:
            existing_jobs = await session.scalar(select(func.count()).select_from(GenerationJob))
        if existing_jobs:
            await self.database.close()
            self.skipTest("fixture database must start without generation jobs")
        self.repository = GenerationRepository(self.database)
        self.source_id = uuid4()
        self.revision_id = uuid4()
        self.revision_ids = {self.revision_id}
        self.job_ids: set[UUID] = set()
        self.page_ids: set[UUID] = set()
        self.evidence_ids: set[UUID] = set()
        self.indexed_at = datetime.now(UTC)
        self.source_hash = hashlib.sha256(f"source-{self.source_id}".encode()).hexdigest()
        self.parsed_hash = hashlib.sha256(b"Verified source text.").hexdigest()
        self.vault_path = f"Sources/Files/{self.source_id}.txt"

        async with self.database.session_factory() as session, session.begin():
            source = SourceDocument(
                id=self.source_id,
                filename="fixture.txt",
                state="active",
            )
            session.add(source)
            await session.flush()
            session.add(
                SourceRevision(
                    id=self.revision_id,
                    source_id=self.source_id,
                    filename="fixture.txt",
                    sha256=self.source_hash,
                    vault_path=self.vault_path,
                    media_type="text/plain",
                    index_state="ready",
                    parsed_text_sha256=self.parsed_hash,
                    parsed_segments=[],
                    indexed_at=self.indexed_at,
                )
            )
            await session.flush()
            source.latest_revision_id = self.revision_id
            source.current_revision_id = self.revision_id

    async def asyncTearDown(self):
        if not getattr(self, "database", None) or not self.database.is_ready:
            return
        async with self.database.session_factory() as session, session.begin():
            page_ids = list(self.page_ids)
            job_ids = list(self.job_ids)
            evidence_ids = list(self.evidence_ids)
            source_ids = [self.source_id]
            revision_ids = list(self.revision_ids)
            if page_ids:
                await session.execute(delete(PageEvidence).where(PageEvidence.page_id.in_(page_ids)))
                await session.execute(delete(GeneratedPage).where(GeneratedPage.page_id.in_(page_ids)))
            if job_ids:
                await session.execute(delete(GenerationJob).where(GenerationJob.id.in_(job_ids)))
            if evidence_ids:
                await session.execute(delete(EvidenceRef).where(EvidenceRef.evidence_id.in_(evidence_ids)))
            if page_ids:
                await session.execute(delete(WikiPage).where(WikiPage.id.in_(page_ids)))
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id.in_(source_ids))
                .values(latest_revision_id=None, current_revision_id=None)
            )
            await session.execute(
                delete(SourceRevision).where(SourceRevision.id.in_(revision_ids))
            )
            await session.execute(delete(SourceDocument).where(SourceDocument.id.in_(source_ids)))
        await self.database.close()

    def make_evidence(self, excerpt: str = "Verified source text.") -> Evidence:
        excerpt_hash = hashlib.sha256(excerpt.encode()).hexdigest()
        evidence_id = evidence_identity(self.revision_id, "chunk-fixture", excerpt_hash)
        self.evidence_ids.add(evidence_id)
        return Evidence(
            evidence_id=evidence_id,
            source_id=self.source_id,
            revision_id=self.revision_id,
            filename="fixture.txt",
            vault_path=self.vault_path,
            source_sha256=self.source_hash,
            parsed_text_sha256=self.parsed_hash,
            chunk_id="chunk-fixture",
            excerpt=excerpt,
            excerpt_sha256=excerpt_hash,
            start=0,
            end=len(excerpt),
            page=None,
            heading=None,
            indexed_at=self.indexed_at,
        )

    def make_draft(self, evidence: Evidence) -> dict:
        return {
            "title": "Fixture",
            "sections": [
                {
                    "heading": "Evidence",
                    "claims": [
                        {
                            "key": "claim-1",
                            "text": "Verified source text.",
                            "evidence_ids": [str(evidence.evidence_id)],
                        }
                    ],
                }
            ],
            "related_page_ids": [],
        }

    @staticmethod
    def make_model() -> dict:
        return {
            "name": "fixture-model",
            "provider": "fixture-provider",
            "generated_at": datetime.now(UTC).isoformat(),
        }

    async def queued_job(self) -> dict:
        job = await self.repository.enqueue("fixture topic")
        self.job_ids.add(UUID(job["job_id"]))
        self.assertEqual(
            set(job),
            {
                "job_id",
                "output_page_id",
                "topic",
                "target_page_id",
                "expected_target_sha256",
                "state",
                "phase",
                "attempts",
                "result",
                "output_sha256",
                "error",
                "created_at",
                "updated_at",
            },
        )
        return job

    async def save_result(self, job: dict, owner: UUID) -> tuple[Evidence, dict]:
        evidence = self.make_evidence()
        draft = self.make_draft(evidence)
        self.assertTrue(
            await self.repository.store_result(
                UUID(job["job_id"]),
                owner,
                draft=draft,
                evidence=[evidence],
                model=self.make_model(),
            )
        )
        return evidence, draft

    async def project_draft(self, page_id: UUID, content_sha256: str) -> None:
        self.page_ids.add(page_id)
        async with self.database.session_factory() as session, session.begin():
            session.add(
                WikiPage(
                    id=page_id,
                    vault_path=f"Wiki/Drafts/{page_id}.md",
                    title="Fixture",
                    status="draft",
                    content_sha256=content_sha256,
                    present=True,
                )
            )

    async def set_short_lease(self, job_id: UUID, *, seconds: float = 0.25) -> None:
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(lease_until=datetime.now(UTC) + timedelta(seconds=seconds))
            )

    async def hold_row_lock(self, model, row_id: UUID):
        session = self.database.session_factory()
        await session.begin()
        await session.scalar(
            select(model).where(model.id == row_id).with_for_update()
        )
        return session

    async def test_claim_lease_expiry_and_owner_fencing(self):
        queued = await self.queued_job()
        first_owner, second_owner = uuid4(), uuid4()
        first = await self.repository.claim(first_owner)
        self.assertEqual(first["job_id"], queued["job_id"])
        self.assertEqual(first["attempts"], 1)
        self.assertIsNone(first["result"])
        self.assertIsNone(await self.repository.claim(second_owner))

        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == UUID(queued["job_id"]))
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )

        second = await self.repository.claim(second_owner)
        self.assertEqual(second["output_page_id"], queued["output_page_id"])
        self.assertEqual(second["attempts"], 2)
        self.assertFalse(await self.repository.renew(UUID(queued["job_id"]), first_owner))
        self.assertTrue(await self.repository.renew(UUID(queued["job_id"]), second_owner))
        await self.repository.release_owner(second_owner)
        released = await self.repository.get_job(UUID(queued["job_id"]))
        self.assertEqual(released["state"], "queued")
        async with self.database.session_factory() as session:
            released_row = await session.get(GenerationJob, UUID(queued["job_id"]))
        self.assertIsNone(released_row.lease_owner)
        self.assertIsNone(released_row.lease_until)

    async def test_stale_evidence_rolls_back_completion_transaction(self):
        queued = await self.queued_job()
        owner = uuid4()
        claimed = await self.repository.claim(owner)
        evidence, draft = await self.save_result(claimed, owner)
        content_hash = "a" * 64
        page_id = UUID(queued["output_page_id"])
        await self.project_draft(page_id, content_hash)

        newer_revision_id = uuid4()
        self.revision_ids.add(newer_revision_id)
        async with self.database.session_factory() as session, session.begin():
            session.add(
                SourceRevision(
                    id=newer_revision_id,
                    source_id=self.source_id,
                    filename="newer.txt",
                    sha256=hashlib.sha256(f"newer-{newer_revision_id}".encode()).hexdigest(),
                    vault_path=f"Sources/Files/{newer_revision_id}.txt",
                    media_type="text/plain",
                    index_state="queued",
                )
            )
            await session.flush()
            source = await session.get(SourceDocument, self.source_id)
            source.latest_revision_id = newer_revision_id

        with self.assertRaises(EvidenceUnavailableError):
            await self.repository.complete(
                UUID(queued["job_id"]),
                owner,
                page_id=page_id,
                content_sha256=content_hash,
                claims=[{"key": "claim-1", "evidence_ids": [str(evidence.evidence_id)]}],
            )
        async with self.database.session_factory() as session:
            job = await session.get(GenerationJob, UUID(queued["job_id"]))
            generation = await session.get(GeneratedPage, page_id)
            links = (
                await session.scalars(select(PageEvidence).where(PageEvidence.page_id == page_id))
            ).all()
        self.assertEqual(job.state, "running")
        self.assertEqual(job.result["draft"], draft)
        self.assertIsNone(generation)
        self.assertEqual(links, [])

    async def test_claim_mismatch_rolls_back_then_complete_and_review_hashes(self):
        queued = await self.queued_job()
        owner = uuid4()
        claimed = await self.repository.claim(owner)
        evidence, _ = await self.save_result(claimed, owner)
        content_hash = "b" * 64
        page_id = UUID(queued["output_page_id"])
        await self.project_draft(page_id, content_hash)

        with self.assertRaises(GenerationConflictError):
            await self.repository.complete(
                UUID(queued["job_id"]),
                owner,
                page_id=page_id,
                content_sha256=content_hash,
                claims=[{"key": "wrong", "evidence_ids": [str(evidence.evidence_id)]}],
            )
        async with self.database.session_factory() as session:
            self.assertIsNone(await session.get(GeneratedPage, page_id))
            job = await session.get(GenerationJob, UUID(queued["job_id"]))
            self.assertEqual(job.state, "running")
            self.assertIsNone(job.output_sha256)

        claims = [{"key": "claim-1", "evidence_ids": [str(evidence.evidence_id)]}]
        self.assertTrue(
            await self.repository.complete(
                UUID(queued["job_id"]),
                owner,
                page_id=page_id,
                content_sha256=content_hash,
                claims=claims,
            )
        )
        self.assertEqual(await self.repository.get_evidence(evidence.evidence_id), evidence)
        with self.assertRaises(GenerationConflictError):
            await self.repository.record_review(
                page_id,
                expected_generated_sha256="c" * 64,
                reviewed_sha256="d" * 64,
            )

        async with self.database.session_factory() as session, session.begin():
            page = await session.get(WikiPage, page_id)
            page.content_sha256 = "e" * 64
        with self.assertRaises(GenerationConflictError):
            await self.repository.record_review(
                page_id,
                expected_generated_sha256=content_hash,
                reviewed_sha256="f" * 64,
            )

        reviewed_hash = hashlib.sha256(b"reviewed fixture markdown").hexdigest()
        async with self.database.session_factory() as session, session.begin():
            page = await session.get(WikiPage, page_id)
            page.status = "reviewed"
            page.content_sha256 = reviewed_hash
        await self.repository.record_review(
            page_id,
            expected_generated_sha256=content_hash,
            reviewed_sha256=reviewed_hash,
        )
        generation = await self.repository.get_generation(page_id)
        self.assertEqual(generation["generated_sha256"], content_hash)
        self.assertEqual(generation["reviewed_sha256"], reviewed_hash)
        self.assertIsNotNone(generation["reviewed_at"])

        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == self.revision_id)
                .values(index_state="failed")
            )
        with self.assertRaises(EvidenceUnavailableError):
            await self.repository.record_review(
                page_id,
                expected_generated_sha256=content_hash,
                reviewed_sha256=reviewed_hash,
            )

    async def test_fail_retry_retains_result_and_reserved_identity(self):
        queued = await self.queued_job()
        owner = uuid4()
        claimed = await self.repository.claim(owner)
        evidence, _ = await self.save_result(claimed, owner)
        reserved_id = queued["output_page_id"]
        self.assertTrue(await self.repository.fail(UUID(queued["job_id"]), owner, "model unavailable"))

        retried = await self.repository.retry(UUID(queued["job_id"]))
        self.assertEqual(retried["output_page_id"], reserved_id)
        self.assertEqual(retried["state"], "queued")
        new_owner = uuid4()
        reclaimed = await self.repository.claim(new_owner)
        self.assertEqual(reclaimed["phase"], "projecting")
        self.assertEqual(reclaimed["result"]["evidence"][0]["evidence_id"], str(evidence.evidence_id))
        self.assertEqual(reclaimed["attempts"], 2)

    async def test_store_and_complete_roll_back_after_locks_outlive_lease(self):
        queued = await self.queued_job()
        job_id = UUID(queued["job_id"])
        owner = uuid4()
        await self.repository.claim(owner)
        evidence = self.make_evidence()
        draft = self.make_draft(evidence)

        source_lock = await self.hold_row_lock(SourceDocument, self.source_id)
        await self.set_short_lease(job_id)
        store_task = asyncio.create_task(
            self.repository.store_result(
                job_id,
                owner,
                draft=draft,
                evidence=[evidence],
                model=self.make_model(),
            )
        )
        await asyncio.sleep(0.4)
        await source_lock.rollback()
        await source_lock.close()
        self.assertFalse(await asyncio.wait_for(store_task, timeout=3))
        async with self.database.session_factory() as session:
            stored_job = await session.get(GenerationJob, job_id)
            evidence_row = await session.get(EvidenceRef, evidence.evidence_id)
        self.assertIsNone(stored_job.result)
        self.assertIsNone(evidence_row)

        # Claim the expired running job again, then make the projected page lock wait
        # cross this attempt's lease deadline.
        next_owner = uuid4()
        claimed = await self.repository.claim(next_owner)
        self.assertEqual(claimed["job_id"], str(job_id))
        self.assertTrue(
            await self.repository.store_result(
                job_id,
                next_owner,
                draft=draft,
                evidence=[evidence],
                model=self.make_model(),
            )
        )
        page_id = UUID(queued["output_page_id"])
        content_hash = "a" * 64
        await self.project_draft(page_id, content_hash)
        page_lock = await self.hold_row_lock(WikiPage, page_id)
        await self.set_short_lease(job_id)
        complete_task = asyncio.create_task(
            self.repository.complete(
                job_id,
                next_owner,
                page_id=page_id,
                content_sha256=content_hash,
                claims=[{"key": "claim-1", "evidence_ids": [str(evidence.evidence_id)]}],
            )
        )
        await asyncio.sleep(0.4)
        await page_lock.rollback()
        await page_lock.close()
        self.assertFalse(await asyncio.wait_for(complete_task, timeout=3))
        async with self.database.session_factory() as session:
            stored_job = await session.get(GenerationJob, job_id)
            generated_page = await session.get(GeneratedPage, page_id)
            page_evidence = (
                await session.scalars(select(PageEvidence).where(PageEvidence.page_id == page_id))
            ).all()
        self.assertEqual(stored_job.state, "running")
        self.assertEqual(stored_job.result["draft"], draft)
        self.assertIsNone(generated_page)
        self.assertEqual(page_evidence, [])

    async def test_fail_and_renew_recheck_database_clock_after_job_lock_wait(self):
        failed_job = await self.queued_job()
        failed_job_id = UUID(failed_job["job_id"])
        fail_owner = uuid4()
        await self.repository.claim(fail_owner)
        await self.set_short_lease(failed_job_id)
        job_lock = await self.hold_row_lock(GenerationJob, failed_job_id)
        fail_task = asyncio.create_task(
            self.repository.fail(failed_job_id, fail_owner, "expired failure")
        )
        await asyncio.sleep(0.4)
        await job_lock.rollback()
        await job_lock.close()
        self.assertFalse(await asyncio.wait_for(fail_task, timeout=3))
        self.assertEqual((await self.repository.get_job(failed_job_id))["state"], "running")
        async with self.database.session_factory() as session, session.begin():
            row = await session.get(GenerationJob, failed_job_id)
            row.state = "failed"
            row.phase = "failed"
            row.lease_owner = None
            row.lease_until = None

        renew_job = await self.queued_job()
        renew_job_id = UUID(renew_job["job_id"])
        renew_owner = uuid4()
        claimed = await self.repository.claim(renew_owner)
        self.assertEqual(claimed["job_id"], str(renew_job_id))
        await self.set_short_lease(renew_job_id)
        renew_lock = await self.hold_row_lock(GenerationJob, renew_job_id)
        renew_task = asyncio.create_task(self.repository.renew(renew_job_id, renew_owner))
        await asyncio.sleep(0.4)
        await renew_lock.rollback()
        await renew_lock.close()
        self.assertFalse(await asyncio.wait_for(renew_task, timeout=3))
        async with self.database.session_factory() as session:
            row = await session.get(GenerationJob, renew_job_id)
        self.assertEqual(row.state, "running")
        self.assertEqual(row.lease_owner, renew_owner)
        self.assertLessEqual(row.lease_until, datetime.now(UTC))


if __name__ == "__main__":
    unittest.main()
