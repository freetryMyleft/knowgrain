"""PostgreSQL transaction tests for explicit review persistence.

These tests require the explicitly selected disposable ``knowgrain_test`` database.
Fixture cleanup is restricted to UUIDs created by each test instance.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.generation_repository import GenerationConflictError
from knowgrain.m3_types import (
    Evidence,
    EvidenceUnavailableError,
    evidence_identity,
)
from knowgrain.models import (
    EvidenceRef,
    GeneratedPage,
    GenerationJob,
    PageEvidence,
    PageGenerationBinding,
    ReviewOperation,
    SourceDocument,
    SourceRevision,
    WikiPage,
)
from knowgrain.review_repository import ReviewRepository


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresReviewRepositoryTests(unittest.IsolatedAsyncioTestCase):
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
        self.repository = ReviewRepository(self.database)

        self.source_id = uuid4()
        self.revision_id = uuid4()
        self.revision_ids: set[UUID] = {self.revision_id}
        self.page_ids: set[UUID] = set()
        self.generation_page_ids: set[UUID] = set()
        self.job_ids: set[UUID] = set()
        self.evidence_ids: set[UUID] = set()
        self.operation_ids: set[UUID] = set()
        self.indexed_at = datetime.now(UTC)
        self.source_hash = hashlib.sha256(f"source-{self.source_id}".encode()).hexdigest()
        self.parsed_hash = hashlib.sha256(b"Verified source text.").hexdigest()
        self.vault_path = f"Sources/Files/{self.source_id}.txt"
        async with self.database.session_factory() as session, session.begin():
            session.add(SourceDocument(id=self.source_id, filename="fixture.txt", state="active"))
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
            source = await session.get(SourceDocument, self.source_id)
            source.latest_revision_id = self.revision_id
            source.current_revision_id = self.revision_id

    async def asyncTearDown(self):
        if not getattr(self, "database", None) or not self.database.is_ready:
            return
        async with self.database.session_factory() as session, session.begin():
            page_ids = list(self.page_ids)
            generation_page_ids = list(self.generation_page_ids)
            if page_ids:
                await session.execute(
                    delete(PageGenerationBinding).where(PageGenerationBinding.page_id.in_(page_ids))
                )
            if self.operation_ids:
                await session.execute(
                    delete(ReviewOperation).where(
                        ReviewOperation.operation_id.in_(list(self.operation_ids))
                    )
                )
            if generation_page_ids:
                await session.execute(
                    delete(PageEvidence).where(PageEvidence.page_id.in_(generation_page_ids))
                )
                await session.execute(
                    delete(GeneratedPage).where(GeneratedPage.page_id.in_(generation_page_ids))
                )
            if self.job_ids:
                await session.execute(
                    delete(GenerationJob).where(GenerationJob.id.in_(list(self.job_ids)))
                )
            if self.evidence_ids:
                await session.execute(
                    delete(EvidenceRef).where(EvidenceRef.evidence_id.in_(list(self.evidence_ids)))
                )
            if page_ids:
                await session.execute(delete(WikiPage).where(WikiPage.id.in_(page_ids)))
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id == self.source_id)
                .values(latest_revision_id=None, current_revision_id=None)
            )
            if self.revision_ids:
                await session.execute(
                    delete(SourceRevision).where(
                        SourceRevision.id.in_(list(self.revision_ids))
                    )
                )
            await session.execute(delete(SourceDocument).where(SourceDocument.id == self.source_id))
        await self.database.close()

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def make_evidence(self) -> Evidence:
        excerpt = "Verified source text."
        excerpt_hash = self.digest(excerpt)
        chunk_id = f"fixture-chunk-{uuid4()}"
        evidence_id = evidence_identity(self.revision_id, chunk_id, excerpt_hash)
        self.evidence_ids.add(evidence_id)
        return Evidence(
            evidence_id=evidence_id,
            source_id=self.source_id,
            revision_id=self.revision_id,
            filename="fixture.txt",
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

    @staticmethod
    def make_draft(evidence: Evidence) -> dict:
        return {
            "title": "Fixture",
            "sections": [
                {
                    "heading": "Facts",
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

    async def create_generation(
        self,
        *,
        proposal: bool = False,
        target_status: str = "draft",
        target_page_id: UUID | None = None,
        target_sha256: str | None = None,
    ) -> dict:
        target_id = target_page_id or uuid4()
        is_new_target = target_page_id is None
        generation_page_id = uuid4() if proposal else target_id
        self.page_ids.update({target_id, generation_page_id})
        self.generation_page_ids.add(generation_page_id)
        job_id = uuid4()
        self.job_ids.add(job_id)
        target_sha = target_sha256 or self.digest(f"target-{target_id}")
        generated_sha = self.digest(f"generated-{generation_page_id}")
        reviewed_sha = self.digest(f"reviewed-{uuid4()}")
        target_path = f"Wiki/Pages/{target_id}.md"
        generated_path = f"Wiki/Drafts/{generation_page_id}.md"
        evidence = self.make_evidence()
        draft = self.make_draft(evidence)
        result = {
            "draft": draft,
            "evidence": [evidence.snapshot()],
            "model": {
                "name": "fixture-model",
                "provider": "fixture-provider",
                "generated_at": self.indexed_at.isoformat(),
            },
        }
        async with self.database.session_factory() as session, session.begin():
            if is_new_target:
                session.add(
                    WikiPage(
                        id=target_id,
                        vault_path=target_path,
                        title="Fixture target",
                        status=target_status if proposal else "draft",
                        content_sha256=target_sha if proposal else generated_sha,
                        present=True,
                    )
                )
            if proposal:
                session.add(
                    WikiPage(
                        id=generation_page_id,
                        vault_path=generated_path,
                        title="Fixture proposal",
                        status="draft",
                        content_sha256=generated_sha,
                        present=True,
                    )
                )
            await session.flush()
            session.add(
                GenerationJob(
                    id=job_id,
                    topic="fixture topic",
                    target_page_id=target_id if proposal else None,
                    target_sha256=target_sha if proposal else None,
                    output_page_id=generation_page_id,
                    state="succeeded",
                    phase="completed",
                    attempts=1,
                    result=result,
                    output_sha256=generated_sha,
                )
            )
            await session.flush()
            session.add(
                GeneratedPage(
                    page_id=generation_page_id,
                    generation_job_id=job_id,
                    draft=draft,
                    generated_sha256=generated_sha,
                    proposal_target_page_id=target_id if proposal else None,
                    proposal_target_sha256=target_sha if proposal else None,
                    model=result["model"],
                )
            )
            session.add(
                EvidenceRef(
                    evidence_id=evidence.evidence_id,
                    source_id=evidence.source_id,
                    revision_id=evidence.revision_id,
                    filename=evidence.filename,
                    vault_path=evidence.vault_path,
                    source_sha256=evidence.source_sha256,
                    parsed_text_sha256=evidence.parsed_text_sha256,
                    chunk_id=evidence.chunk_id,
                    excerpt=evidence.excerpt,
                    excerpt_sha256=evidence.excerpt_sha256,
                    start=evidence.start,
                    end=evidence.end,
                    page=evidence.page,
                    heading=evidence.heading,
                    indexed_at=evidence.indexed_at,
                )
            )
            session.add(
                PageEvidence(
                    page_id=generation_page_id,
                    evidence_id=evidence.evidence_id,
                    claim_key="claim-1",
                )
            )
        return {
            "target_id": target_id,
            "generation_page_id": generation_page_id,
            "target_sha256": target_sha,
            "generated_sha256": generated_sha,
            "reviewed_sha256": reviewed_sha,
            "job_id": job_id,
            "draft": draft,
        }

    async def project_reviewed(self, page_id: UUID, content_sha256: str) -> None:
        async with self.database.session_factory() as session, session.begin():
            page = await session.get(WikiPage, page_id)
            page.status = "reviewed"
            page.content_sha256 = content_sha256

    async def prepare_generation(self, generation: dict, operation_id: UUID | None = None) -> UUID:
        operation_id = operation_id or uuid4()
        self.operation_ids.add(operation_id)
        await self.repository.prepare(
            operation_id,
            page_id=generation["target_id"],
            generation_page_id=generation["generation_page_id"],
            expected_page_sha256=generation["target_sha256"]
            if generation["target_id"] != generation["generation_page_id"]
            else generation["generated_sha256"],
            expected_generation_sha256=generation["generated_sha256"],
            reviewed_sha256=generation["reviewed_sha256"],
        )
        return operation_id

    async def change_source_to_new_revision(self) -> None:
        revision_id = uuid4()
        self.revision_ids.add(revision_id)
        async with self.database.session_factory() as session, session.begin():
            session.add(
                SourceRevision(
                    id=revision_id,
                    source_id=self.source_id,
                    filename="newer.txt",
                    sha256=self.digest(f"new revision-{revision_id}"),
                    vault_path=f"Sources/Files/{revision_id}.txt",
                    media_type="text/plain",
                    index_state="queued",
                )
            )
            await session.flush()
            source = await session.get(SourceDocument, self.source_id)
            source.latest_revision_id = revision_id

    async def test_direct_review_preparation_completion_and_idempotence(self):
        generated = await self.create_generation()
        operation_id = await self.prepare_generation(generated)
        prepared = await self.repository.prepare(
            operation_id,
            page_id=generated["target_id"],
            generation_page_id=generated["generation_page_id"],
            expected_page_sha256=generated["generated_sha256"],
            expected_generation_sha256=generated["generated_sha256"],
            reviewed_sha256=generated["reviewed_sha256"],
        )
        self.assertEqual(prepared["state"], "prepared")

        await self.project_reviewed(generated["target_id"], generated["reviewed_sha256"])
        retried = await self.repository.prepare(
            operation_id,
            page_id=generated["target_id"],
            generation_page_id=generated["generation_page_id"],
            expected_page_sha256=generated["generated_sha256"],
            expected_generation_sha256=generated["generated_sha256"],
            reviewed_sha256=generated["reviewed_sha256"],
        )
        self.assertEqual(retried["state"], "prepared")

        completed = await self.repository.complete(operation_id)
        repeated = await self.repository.complete(operation_id)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(repeated, completed)
        binding = await self.repository.get_binding(generated["target_id"])
        self.assertEqual(binding["generation_page_id"], str(generated["generation_page_id"]))
        self.assertEqual(binding["reviewed_sha256"], generated["reviewed_sha256"])
        self.assertEqual(binding["operation_id"], str(operation_id))

        async with self.database.session_factory() as session:
            manifest = await session.get(GeneratedPage, generated["generation_page_id"])
        self.assertEqual(manifest.reviewed_sha256, generated["reviewed_sha256"])
        self.assertIsNotNone(manifest.reviewed_at)

        completed_prepare = await self.repository.prepare(
            operation_id,
            page_id=generated["target_id"],
            generation_page_id=generated["generation_page_id"],
            expected_page_sha256=generated["generated_sha256"],
            expected_generation_sha256=generated["generated_sha256"],
            reviewed_sha256=generated["reviewed_sha256"],
        )
        self.assertEqual(completed_prepare["state"], "completed")

        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(SourceRevision)
                .where(SourceRevision.id == self.revision_id)
                .values(index_state="failed")
            )
        with self.assertRaises(EvidenceUnavailableError):
            await self.repository.prepare(
                operation_id,
                page_id=generated["target_id"],
                generation_page_id=generated["generation_page_id"],
                expected_page_sha256=generated["generated_sha256"],
                expected_generation_sha256=generated["generated_sha256"],
                reviewed_sha256=generated["reviewed_sha256"],
            )
        with self.assertRaises(EvidenceUnavailableError):
            await self.repository.complete(operation_id)

    async def test_proposal_binds_target_without_changing_proposal_manifest(self):
        generated = await self.create_generation(proposal=True, target_status="reviewed")
        original_draft = generated["draft"]
        operation_id = await self.prepare_generation(generated)
        await self.project_reviewed(generated["target_id"], generated["reviewed_sha256"])
        result = await self.repository.complete(operation_id)
        self.assertEqual(result["state"], "completed")

        binding = await self.repository.get_binding(generated["target_id"])
        self.assertEqual(binding["generation_page_id"], str(generated["generation_page_id"]))
        async with self.database.session_factory() as session:
            proposal = await session.get(GeneratedPage, generated["generation_page_id"])
            proposal_page = await session.get(WikiPage, generated["generation_page_id"])
        self.assertEqual(proposal.draft, original_draft)
        self.assertEqual(proposal.generated_sha256, generated["generated_sha256"])
        self.assertEqual(proposal.proposal_target_page_id, generated["target_id"])
        self.assertIsNone(proposal.reviewed_at)
        self.assertEqual(proposal_page.status, "draft")
        self.assertEqual(proposal_page.content_sha256, generated["generated_sha256"])

    async def test_stale_evidence_is_rechecked_on_prepared_retry_and_completion(self):
        generated = await self.create_generation()
        operation_id = await self.prepare_generation(generated)
        await self.change_source_to_new_revision()

        with self.assertRaises(EvidenceUnavailableError):
            await self.prepare_generation(generated, operation_id)
        await self.project_reviewed(generated["target_id"], generated["reviewed_sha256"])
        with self.assertRaises(EvidenceUnavailableError):
            await self.repository.complete(operation_id)
        self.assertIsNone(await self.repository.get_binding(generated["target_id"]))

    async def test_late_prepared_operation_cannot_roll_binding_back(self):
        generated = await self.create_generation(proposal=True)
        newer_generated = await self.create_generation(
            proposal=True,
            target_page_id=generated["target_id"],
            target_sha256=generated["target_sha256"],
        )

        # Use two proposal manifests for the same target. Both prepare against the same
        # old hash, then the newer operation completes before the delayed older one.

        older_op, newer_op = uuid4(), uuid4()
        self.operation_ids.update({older_op, newer_op})
        await self.prepare_generation(generated, older_op)
        await self.prepare_generation(newer_generated, newer_op)
        async with self.database.session_factory() as session, session.begin():
            older = await session.get(ReviewOperation, older_op)
            older.created_at -= timedelta(days=1)

        await self.project_reviewed(generated["target_id"], newer_generated["reviewed_sha256"])
        await self.repository.complete(newer_op)
        binding_before = await self.repository.get_binding(generated["target_id"])

        # A delayed old file retry can leave its projection hash visible; completion
        # must still honor the newer durable binding and refuse to replace it.
        await self.project_reviewed(generated["target_id"], generated["reviewed_sha256"])
        with self.assertRaises(GenerationConflictError):
            await self.repository.complete(older_op)
        binding_after = await self.repository.get_binding(generated["target_id"])
        self.assertEqual(binding_after, binding_before)
        self.assertEqual(binding_after["operation_id"], str(newer_op))

    async def test_operation_id_cannot_be_reused_with_changed_hashes(self):
        generated = await self.create_generation()
        operation_id = await self.prepare_generation(generated)
        with self.assertRaises(GenerationConflictError):
            await self.repository.prepare(
                operation_id,
                page_id=generated["target_id"],
                generation_page_id=generated["generation_page_id"],
                expected_page_sha256=generated["generated_sha256"],
                expected_generation_sha256=generated["generated_sha256"],
                reviewed_sha256=self.digest("different reviewed body"),
            )
