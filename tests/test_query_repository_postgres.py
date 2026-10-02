"""PostgreSQL persistence checks for durable M4 query jobs.

These tests require the explicitly selected disposable ``knowgrain_test`` DB.
Cleanup is restricted to UUIDs created by this test instance.
"""

from __future__ import annotations

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
from knowgrain.m3_types import Evidence, EvidenceUnavailableError, evidence_identity
from knowgrain.models import EvidenceRef, QueryJob, SourceDocument, SourceRevision
from knowgrain.query_repository import QueryConflictError, QueryRepository


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")
INSUFFICIENT_MESSAGE = "无法核实：当前资料不足以支持该问题。"


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresQueryRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
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
            outstanding = await session.scalar(select(func.count()).select_from(QueryJob))
        if outstanding:
            await self.database.close()
            self.skipTest("fixture database must start without query jobs")

        self.repository = QueryRepository(self.database)
        self.source_id = uuid4()
        self.revision_id = uuid4()
        self.job_ids: set[UUID] = set()
        self.evidence_ids: set[UUID] = set()
        self.indexed_at = datetime.now(UTC)
        self.original_text = "A verified retained sentence."
        self.source_hash = hashlib.sha256(self.original_text.encode()).hexdigest()
        self.parsed_hash = hashlib.sha256(self.original_text.encode()).hexdigest()
        self.vault_path = f"Sources/Files/{self.source_id}/{self.revision_id}.txt"
        async with self.database.session_factory() as session, session.begin():
            session.add(SourceDocument(id=self.source_id, filename="query-fixture.txt", state="active"))
            await session.flush()
            session.add(
                SourceRevision(
                    id=self.revision_id,
                    source_id=self.source_id,
                    filename="query-fixture.txt",
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
            source = await session.get(SourceDocument, self.source_id)
            source.latest_revision_id = self.revision_id
            source.current_revision_id = self.revision_id

    async def asyncTearDown(self) -> None:
        if not getattr(self, "database", None) or not self.database.is_ready:
            return
        async with self.database.session_factory() as session, session.begin():
            if self.job_ids:
                await session.execute(delete(QueryJob).where(QueryJob.id.in_(list(self.job_ids))))
            if self.evidence_ids:
                await session.execute(
                    delete(EvidenceRef).where(EvidenceRef.evidence_id.in_(list(self.evidence_ids)))
                )
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id == self.source_id)
                .values(latest_revision_id=None, current_revision_id=None)
            )
            await session.execute(delete(SourceRevision).where(SourceRevision.id == self.revision_id))
            await session.execute(delete(SourceDocument).where(SourceDocument.id == self.source_id))
        await self.database.close()

    def evidence(self) -> Evidence:
        excerpt = "verified retained sentence"
        excerpt_hash = hashlib.sha256(excerpt.encode()).hexdigest()
        chunk_id = f"chunk-{uuid4()}"
        evidence_id = evidence_identity(self.revision_id, chunk_id, excerpt_hash)
        self.evidence_ids.add(evidence_id)
        return Evidence(
            evidence_id=evidence_id,
            source_id=self.source_id,
            revision_id=self.revision_id,
            filename="query-fixture.txt",
            vault_path=self.vault_path,
            source_sha256=self.source_hash,
            parsed_text_sha256=self.parsed_hash,
            chunk_id=chunk_id,
            excerpt=excerpt,
            excerpt_sha256=excerpt_hash,
            start=0,
            end=len(excerpt),
            page=None,
            heading=None,
            indexed_at=self.indexed_at,
        )

    async def test_answer_completion_retains_only_cited_evidence(self) -> None:
        queued = await self.repository.enqueue("What is supported?")
        job_id = UUID(queued["job_id"])
        self.job_ids.add(job_id)
        listed = await self.repository.list_jobs()
        self.assertNotIn("result", listed[0])
        owner = uuid4()
        claimed = await self.repository.claim_next(owner)
        self.assertEqual(claimed["job_id"], str(job_id))
        evidence = self.evidence()
        result = {
            "status": "answered",
            "message": "",
            "claims": [
                {
                    "key": "claim.fact",
                    "text": "The source contains the retained sentence.",
                    "evidence_ids": [str(evidence.evidence_id)],
                }
            ],
            "model": {
                "name": "test-model",
                "provider": "test-provider",
                "generated_at": datetime.now(UTC).isoformat(),
            },
        }

        self.assertTrue(await self.repository.complete(job_id, owner, result, (evidence,)))
        saved = await self.repository.get_job(job_id)
        self.assertEqual(saved["state"], "succeeded")
        self.assertEqual(saved["result"]["evidence"], [evidence.snapshot()])
        self.assertEqual(saved["result"]["claims"][0]["evidence_ids"], [str(evidence.evidence_id)])
        self.assertFalse(await self.repository.complete(job_id, owner, result, (evidence,)))
        async with self.database.session_factory() as session:
            row = await session.get(EvidenceRef, evidence.evidence_id)
        self.assertIsNotNone(row)

    async def test_insufficient_result_is_durable_without_evidence(self) -> None:
        queued = await self.repository.enqueue("Question without support")
        job_id = UUID(queued["job_id"])
        self.job_ids.add(job_id)
        owner = uuid4()
        await self.repository.claim_next(owner)
        result = {
            "status": "insufficient",
            "message": INSUFFICIENT_MESSAGE,
            "claims": [],
            "model": {
                "name": "configured-query-model",
                "provider": "ollama",
                "generated_at": datetime.now(UTC).isoformat(),
            },
        }

        self.assertTrue(await self.repository.complete(job_id, owner, result, ()))
        saved = await self.repository.get_job(job_id)
        self.assertEqual(saved["result"]["status"], "insufficient")
        self.assertEqual(saved["result"]["evidence"], [])
        self.assertEqual(saved["result"]["claims"], [])

    async def test_fail_retry_and_expired_lease_reclaim(self) -> None:
        queued = await self.repository.enqueue("Retry this question")
        job_id = UUID(queued["job_id"])
        self.job_ids.add(job_id)
        first_owner = uuid4()
        await self.repository.claim_next(first_owner)
        with self.assertRaises(QueryConflictError):
            await self.repository.retry(job_id)
        self.assertTrue(await self.repository.fail(job_id, first_owner, "temporary provider error"))
        retried = await self.repository.retry(job_id)
        self.assertEqual(retried["state"], "queued")
        second_owner = uuid4()
        claimed = await self.repository.claim_next(second_owner)
        self.assertEqual(claimed["attempts"], 2)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(QueryJob)
                .where(QueryJob.id == job_id)
                .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
            )
        third_owner = uuid4()
        reclaimed = await self.repository.claim_next(third_owner)
        self.assertEqual(reclaimed["job_id"], str(job_id))
        self.assertEqual(reclaimed["attempts"], 3)

    async def test_stale_evidence_does_not_commit_result_or_reference(self) -> None:
        queued = await self.repository.enqueue("Question with stale evidence")
        job_id = UUID(queued["job_id"])
        self.job_ids.add(job_id)
        owner = uuid4()
        await self.repository.claim_next(owner)
        evidence = self.evidence()
        result = {
            "status": "answered",
            "message": "",
            "claims": [
                {"key": "claim-1", "text": "A supported fact.", "evidence_ids": [str(evidence.evidence_id)]}
            ],
            "model": {
                "name": "test-model",
                "provider": "test-provider",
                "generated_at": datetime.now(UTC).isoformat(),
            },
        }
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == self.revision_id)
                .values(index_state="failed")
            )

        with self.assertRaises(EvidenceUnavailableError):
            await self.repository.complete(job_id, owner, result, (evidence,))
        saved = await self.repository.get_job(job_id)
        self.assertEqual(saved["state"], "running")
        self.assertIsNone(saved["result"])
        async with self.database.session_factory() as session:
            row = await session.get(EvidenceRef, evidence.evidence_id)
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
