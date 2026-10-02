"""PostgreSQL candidate-query tests against only an explicitly selected test DB."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, update

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase
from knowgrain.entity_mapping_repository import EntityMappingRepository
from knowgrain.m3_types import Evidence, evidence_identity
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


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresEntityMappingRepositoryTests(unittest.IsolatedAsyncioTestCase):
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
        self.repository = EntityMappingRepository(self.database)
        self.source_id = uuid4()
        self.revision_id = uuid4()
        self.old_chunk_id = f"old-manifest-{uuid4()}"
        self.selected_chunk_id = f"selected-manifest-{uuid4()}"
        self.page_ids: set[UUID] = set()
        self.generation_ids: set[UUID] = set()
        self.job_ids: set[UUID] = set()
        self.evidence_ids: set[UUID] = set()
        self.operation_ids: set[UUID] = set()
        self.indexed_at = datetime.now(UTC)
        self.source_hash = hashlib.sha256(f"source-{self.source_id}".encode()).hexdigest()
        self.parsed_hash = hashlib.sha256(b"Repository test source text.").hexdigest()
        self.source_path = f"Sources/entity-mapping-{self.source_id}.txt"
        async with self.database.session_factory() as session, session.begin():
            source = SourceDocument(id=self.source_id, filename="entity-mapping.txt", state="active")
            session.add(source)
            await session.flush()
            session.add(SourceRevision(
                id=self.revision_id,
                source_id=self.source_id,
                filename="entity-mapping.txt",
                sha256=self.source_hash,
                vault_path=self.source_path,
                media_type="text/plain",
                index_state="ready",
                parsed_text_sha256=self.parsed_hash,
                parsed_segments=[],
                indexed_at=self.indexed_at,
            ))
            await session.flush()
            source.latest_revision_id = self.revision_id
            source.current_revision_id = self.revision_id

        self.target_id = uuid4()
        self.proposal_id = uuid4()
        self.other_id = uuid4()
        self.page_ids.update({self.target_id, self.proposal_id, self.other_id})
        self.old_evidence = self.make_evidence(self.old_chunk_id)
        self.current_evidence = self.make_evidence(self.selected_chunk_id)
        self.other_evidence = self.make_evidence(self.selected_chunk_id)
        await self._insert_manifest(self.target_id, "Target")
        await self._insert_manifest(self.proposal_id, "Proposal")
        await self._insert_manifest(self.other_id, "Other")
        await self._bind_target_to_proposal()

    async def asyncTearDown(self):
        if not getattr(self, "database", None) or not self.database.is_ready:
            return
        async with self.database.session_factory() as session, session.begin():
            if self.page_ids:
                await session.execute(
                    delete(PageGenerationBinding).where(
                        PageGenerationBinding.page_id.in_(list(self.page_ids))
                    )
                )
            if self.operation_ids:
                await session.execute(
                    delete(ReviewOperation).where(
                        ReviewOperation.operation_id.in_(list(self.operation_ids))
                    )
                )
            if self.generation_ids:
                await session.execute(
                    delete(PageEvidence).where(
                        PageEvidence.page_id.in_(list(self.generation_ids))
                    )
                )
                await session.execute(
                    delete(GeneratedPage).where(
                        GeneratedPage.page_id.in_(list(self.generation_ids))
                    )
                )
            if self.job_ids:
                await session.execute(
                    delete(GenerationJob).where(GenerationJob.id.in_(list(self.job_ids)))
                )
            if self.evidence_ids:
                await session.execute(
                    delete(EvidenceRef).where(
                        EvidenceRef.evidence_id.in_(list(self.evidence_ids))
                    )
                )
            if self.page_ids:
                await session.execute(delete(WikiPage).where(WikiPage.id.in_(list(self.page_ids))))
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id == self.source_id)
                .values(latest_revision_id=None, current_revision_id=None)
            )
            await session.execute(
                delete(SourceRevision).where(SourceRevision.id == self.revision_id)
            )
            await session.execute(delete(SourceDocument).where(SourceDocument.id == self.source_id))
        await self.database.close()

    def make_evidence(self, chunk_id: str) -> Evidence:
        excerpt = f"Source quotation for {chunk_id} {uuid4()}."
        excerpt_hash = hashlib.sha256(excerpt.encode()).hexdigest()
        evidence_id = evidence_identity(self.revision_id, chunk_id, excerpt_hash)
        self.evidence_ids.add(evidence_id)
        return Evidence(
            evidence_id=evidence_id,
            source_id=self.source_id,
            revision_id=self.revision_id,
            filename="entity-mapping.txt",
            vault_path=self.source_path,
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

    async def _insert_manifest(self, page_id: UUID, title: str) -> None:
        evidence = {
            self.target_id: self.old_evidence,
            self.proposal_id: self.current_evidence,
            self.other_id: self.other_evidence,
        }[page_id]
        job_id = uuid4()
        self.job_ids.add(job_id)
        self.generation_ids.add(page_id)
        content_hash = hashlib.sha256(f"{title}-{page_id}".encode()).hexdigest()
        model = {"name": "fixture", "provider": "fixture", "generated_at": self.indexed_at.isoformat()}
        async with self.database.session_factory() as session, session.begin():
            session.add(WikiPage(
                id=page_id,
                vault_path=f"Wiki/Pages/{page_id}.md",
                title=title,
                status="reviewed" if page_id == self.target_id else "draft",
                content_sha256=content_hash,
                present=True,
            ))
            await session.flush()
            session.add(GenerationJob(
                id=job_id,
                topic=f"fixture {title}",
                output_page_id=page_id,
                state="succeeded",
                phase="completed",
                attempts=1,
                result={"draft": {}, "evidence": [evidence.snapshot()], "model": model},
                output_sha256=content_hash,
            ))
            await session.flush()
            session.add(GeneratedPage(
                page_id=page_id,
                generation_job_id=job_id,
                draft={},
                generated_sha256=content_hash,
                model=model,
            ))
            session.add(EvidenceRef(
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
            ))
            session.add(PageEvidence(
                page_id=page_id,
                evidence_id=evidence.evidence_id,
                claim_key="claim-1",
            ))
            if page_id == self.proposal_id:
                # Repeated use of the same exact citation across claims must
                # still yield one deterministic page candidate.
                session.add(PageEvidence(
                    page_id=page_id,
                    evidence_id=evidence.evidence_id,
                    claim_key="claim-2",
                ))

    async def _bind_target_to_proposal(self) -> None:
        operation_id = uuid4()
        self.operation_ids.add(operation_id)
        reviewed_hash = hashlib.sha256(f"reviewed-{self.target_id}".encode()).hexdigest()
        async with self.database.session_factory() as session, session.begin():
            session.add(ReviewOperation(
                operation_id=operation_id,
                page_id=self.target_id,
                generation_page_id=self.proposal_id,
                expected_page_sha256=hashlib.sha256(b"expected-page").hexdigest(),
                expected_generation_sha256=hashlib.sha256(b"expected-generation").hexdigest(),
                reviewed_sha256=reviewed_hash,
                state="completed",
                completed_at=self.indexed_at,
            ))
            await session.flush()
            session.add(PageGenerationBinding(
                page_id=self.target_id,
                generation_page_id=self.proposal_id,
                reviewed_sha256=reviewed_hash,
                reviewed_at=self.indexed_at,
                operation_id=operation_id,
            ))

    async def test_binding_selects_current_manifest_and_never_uses_old_own_manifest(self):
        selected = await self.repository.candidate_page_ids(
            [self.selected_chunk_id], limit=50
        )
        self.assertEqual(set(selected["page_ids"]), {self.target_id, self.proposal_id, self.other_id})
        self.assertEqual(len(selected["page_ids"]), len(set(selected["page_ids"])))
        self.assertFalse(selected["truncated"])

        old_only = await self.repository.candidate_page_ids([self.old_chunk_id], limit=50)
        self.assertNotIn(self.target_id, old_only["page_ids"])
        self.assertEqual(old_only["page_ids"], ())

        unrelated = await self.repository.candidate_page_ids(["unrelated-chunk"], limit=50)
        self.assertEqual(unrelated["page_ids"], ())

    async def test_query_is_bounded_and_reports_limit_plus_one_truncation(self):
        result = await self.repository.candidate_page_ids(
            [self.selected_chunk_id], limit=2
        )
        self.assertEqual(len(result["page_ids"]), 2)
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
