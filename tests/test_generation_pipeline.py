"""Integrated M3 generation checks using real Vault files and in-memory repositories.

The model and repository doubles make these deterministic filesystem/service tests;
they do not prove PostgreSQL transaction behavior or output quality from a real model.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx

from knowgrain.config import Settings
from knowgrain.generation_api import install_generation_routes
from knowgrain.generation_service import GenerationService
from knowgrain.m3_types import (
    EligibleRevision,
    Evidence,
    EvidenceUnavailableError,
    evidence_identity,
)
from knowgrain.parsers import parse_document
from knowgrain.provenance import ProvenanceService
from knowgrain.vault import VaultStore
from knowgrain.wiki_files import WikiConflictError, parse_wiki
from knowgrain.wiki_service import WikiService


class InMemoryProvenanceRepository:
    """Current-revision lookup double for ProvenanceService."""

    def __init__(self, revision: EligibleRevision) -> None:
        self.revisions_by_path = {revision.vault_path: revision}
        self.revisions_by_id = {revision.revision_id: revision}

    async def eligible_by_paths(self, paths):
        return {
            path: self.revisions_by_path[path]
            for path in paths
            if path in self.revisions_by_path
        }

    async def eligible_by_ids(self, ids):
        return {
            revision_id: self.revisions_by_id[revision_id]
            for revision_id in ids
            if revision_id in self.revisions_by_id
        }

    def invalidate(self) -> None:
        self.revisions_by_path.clear()
        self.revisions_by_id.clear()


class InMemoryWikiRepository:
    """Small projection double; WikiService and WikiFileStore remain real."""

    def __init__(self) -> None:
        self.pages = {}

    async def replace_projection(self, pages) -> None:
        self.pages = {page.page_id: page for page in pages}

    async def list_pages(self, limit: int = 100, offset: int = 0):
        pages = sorted(
            self.pages.values(),
            key=lambda page: (page.title, page.vault_path, str(page.page_id)),
        )
        return [self._summary(page) for page in pages[offset : offset + limit]]

    async def get_page(self, page_id: UUID):
        page = self.pages.get(page_id)
        if page is None:
            return None
        return {
            **self._summary(page),
            "links": [
                {
                    "target": link.target,
                    "anchor": link.anchor,
                    "label": link.label,
                    "embed": link.embed,
                    "line": link.line,
                    "to_page_id": None,
                }
                for link in page.links
            ],
        }

    async def backlinks(self, page_id: UUID):
        return []

    @staticmethod
    def _summary(page):
        return {
            "page_id": str(page.page_id),
            "vault_path": page.vault_path,
            "title": page.title,
            "status": page.status,
            "content_sha256": page.content_sha256,
            "updated_at": datetime(2026, 10, 1, tzinfo=UTC),
        }


class InMemoryGenerationRepository:
    """Retained generation snapshot double for GenerationService integration tests."""

    def __init__(self, job_id: UUID, output_page_id: UUID) -> None:
        self.database = type("ReadyDatabase", (), {"is_ready": True})()
        self.job_id = job_id
        self.output_page_id = output_page_id
        self.result = None
        self.stored_evidence: dict[UUID, Evidence] = {}
        self.completed = None
        self.store_result_calls = 0

    def job(self):
        return {
            "job_id": str(self.job_id),
            "output_page_id": str(self.output_page_id),
            "topic": "A source-backed topic",
            "target_page_id": None,
            "expected_target_sha256": None,
            "result": self.result,
        }

    async def store_result(self, job_id, owner, *, draft, evidence, model):
        assert job_id == self.job_id
        self.store_result_calls += 1
        self.stored_evidence = {item.evidence_id: item for item in evidence}
        self.result = {
            "draft": draft,
            "evidence": [item.snapshot() for item in evidence],
            "model": model,
        }
        return True

    async def complete(self, job_id, owner, *, page_id, content_sha256, claims):
        assert job_id == self.job_id
        self.completed = {
            "page_id": page_id,
            "content_sha256": content_sha256,
            "claims": claims,
        }
        return True

    async def get_generation(self, page_id):
        if self.completed is None or page_id != self.completed["page_id"]:
            return None
        return {
            "page_id": str(page_id),
            "draft": self.result["draft"],
            "evidence": self.result["evidence"],
            "model": self.result["model"],
            "generated_sha256": self.completed["content_sha256"],
            "reviewed_sha256": None,
            "reviewed_at": None,
        }

    async def get_evidence(self, evidence_id):
        return self.stored_evidence.get(evidence_id)


class DeterministicCore:
    """Stable model/Core double; no network or real model is involved."""

    def __init__(self, vault_path: str, excerpt: str, evidence_id: UUID) -> None:
        self.vault_path = vault_path
        self.excerpt = excerpt
        self.evidence_id = evidence_id
        self.retrieve_calls = 0
        self.generate_calls = 0
        self.prompts = []
        self.on_generate = None

    async def retrieve(self, topic, *, mode):
        self.retrieve_calls += 1
        assert mode == "mix"
        return {
            "status": "success",
            "data": {
                "chunks": [
                    {
                        "file_path": self.vault_path,
                        "chunk_id": "fixture-chunk",
                        "content": self.excerpt,
                    }
                ],
                "entities": [],
                "relationships": [],
            },
        }

    async def generate_json(self, system, prompt):
        self.generate_calls += 1
        self.prompts.append((system, prompt))
        if self.on_generate is not None:
            value = self.on_generate()
            if asyncio.iscoroutine(value):
                await value
        return json.dumps(
            {
                "title": "Verified draft",
                "sections": [
                    {
                        "heading": "Findings",
                        "claims": [
                            {
                                "key": "claim-1",
                                "text": "The source records a verified fact.",
                                "evidence_ids": [str(self.evidence_id)],
                            }
                        ],
                    }
                ],
                "related_page_ids": [],
            }
        )


class GenerationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name) / "vault")
        self.vault.initialize()

        excerpt = "The source records a verified fact."
        source_bytes = excerpt.encode("utf-8")
        source_id, revision_id = uuid4(), uuid4()
        vault_path = self.vault.write_source(
            source_id, revision_id, "fixture.txt", source_bytes
        )
        parsed = parse_document("fixture.txt", source_bytes)
        revision = EligibleRevision(
            source_id=source_id,
            revision_id=revision_id,
            filename="fixture.txt",
            vault_path=vault_path,
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            parsed_text_sha256=hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
            indexed_at=datetime(2026, 9, 30, tzinfo=UTC),
        )
        self.source_path = self.vault.resolve(vault_path)
        self.provenance_repository = InMemoryProvenanceRepository(revision)
        self.provenance = ProvenanceService(self.provenance_repository, self.vault)

        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        evidence_id = evidence_identity(revision_id, "fixture-chunk", excerpt_hash)
        self.core = DeterministicCore(vault_path, excerpt, evidence_id)
        self.wiki_repository = InMemoryWikiRepository()
        self.wiki = WikiService(self.vault, self.wiki_repository)
        self.generation_repository = InMemoryGenerationRepository(uuid4(), uuid4())
        self.service = GenerationService(
            Settings(_env_file=None),
            self.generation_repository,
            self.provenance,
            self.core,
            self.wiki,
        )

    async def test_result_is_retained_before_publication_and_retry_reuses_identity(self):
        real_publish = self.service.files.publish
        publication_page_ids = []

        def interrupted_publish(page_id, markdown, evidence_pages):
            publication_page_ids.append(page_id)
            self.assertIsNotNone(self.generation_repository.result)
            self.assertEqual(len(self.generation_repository.result["evidence"]), 1)
            raise OSError("simulated publication interruption")

        self.service.files.publish = interrupted_publish
        with self.assertRaisesRegex(OSError, "simulated publication interruption"):
            await self.service._process(self.generation_repository.job())

        self.assertEqual(self.generation_repository.store_result_calls, 1)
        self.assertFalse(
            self.vault.resolve(
                f"Wiki/Drafts/{self.generation_repository.output_page_id}.md"
            ).exists()
        )

        def retry_publish(page_id, markdown, evidence_pages):
            publication_page_ids.append(page_id)
            return real_publish(page_id, markdown, evidence_pages)

        self.service.files.publish = retry_publish
        await self.service._process(self.generation_repository.job())

        self.assertEqual(self.core.retrieve_calls, 1)
        self.assertEqual(self.core.generate_calls, 1)
        self.assertEqual(self.generation_repository.store_result_calls, 1)
        self.assertEqual(
            publication_page_ids,
            [self.generation_repository.output_page_id] * 2,
        )
        self.assertEqual(
            self.generation_repository.completed["page_id"],
            self.generation_repository.output_page_id,
        )
        page_path = self.vault.resolve(
            f"Wiki/Drafts/{self.generation_repository.output_page_id}.md"
        )
        relative_page_path = page_path.relative_to(self.vault.root).as_posix()
        page = parse_wiki(page_path.read_text(encoding="utf-8"), relative_page_path)
        self.assertEqual(page.page_id, self.generation_repository.output_page_id)
        self.assertEqual(page.status, "draft")
        self.assertTrue(
            self.vault.resolve(
                f"Sources/Evidence/{self.core.evidence_id}.md"
            ).is_file()
        )

    async def test_retained_retry_does_not_overwrite_manual_page_hash_change(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        page_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        edited_markdown = page_path.read_text(encoding="utf-8") + "\nHuman edit.\n"
        page_path.write_text(edited_markdown, encoding="utf-8")

        with self.assertRaises(WikiConflictError):
            await self.service._process(self.generation_repository.job())

        self.assertEqual(page_path.read_text(encoding="utf-8"), edited_markdown)
        self.assertEqual(self.core.retrieve_calls, 1)
        self.assertEqual(self.core.generate_calls, 1)
        self.assertEqual(self.generation_repository.output_page_id, page_id)

    async def test_original_changed_during_inference_blocks_publication(self):
        self.core.on_generate = lambda: self.source_path.write_bytes(
            b"Changed during model inference."
        )
        await self._assert_stale_result_blocks()

    async def test_lost_current_revision_eligibility_blocks_publication(self):
        self.core.on_generate = self.provenance_repository.invalidate
        await self._assert_stale_result_blocks()

    async def _assert_stale_result_blocks(self):
        with self.assertRaises(EvidenceUnavailableError):
            await self.service._process(self.generation_repository.job())

        page_path = self.vault.resolve(
            f"Wiki/Drafts/{self.generation_repository.output_page_id}.md"
        )
        evidence_path = self.vault.resolve(
            f"Sources/Evidence/{self.core.evidence_id}.md"
        )
        self.assertFalse(page_path.exists())
        self.assertFalse(evidence_path.exists())
        self.assertIsNone(self.generation_repository.result)
        self.assertIsNone(self.generation_repository.completed)

    async def test_inspection_and_evidence_api_label_stale_source_as_not_current(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        evidence_id = self.core.evidence_id
        self.source_path.write_bytes(b"Original changed after generation.")

        app = FastAPI()
        install_generation_routes(app)
        app.state.runtime = type(
            "Runtime",
            (),
            {
                "_runtime_lock": asyncio.Lock(),
                "database": type("Database", (), {"is_ready": True})(),
                "vault_ready": True,
                "generation": self.service,
            },
        )()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://knowgrain.test"
        ) as client:
            generation_response = await client.get(
                f"/api/v1/wiki/pages/{page_id}/generation"
            )
            evidence_response = await client.get(f"/api/v1/evidence/{evidence_id}")

        self.assertEqual(generation_response.status_code, 200)
        self.assertFalse(generation_response.json()["evidence_current"])
        self.assertEqual(evidence_response.status_code, 200)
        self.assertFalse(evidence_response.json()["current"])
        self.assertEqual(evidence_response.json()["evidence_id"], str(evidence_id))

    async def test_large_related_catalog_respects_json_byte_and_count_bounds(self):
        related_directory = self.vault.resolve(
            "Wiki/Pages/" + "/".join(["a" * 90, "b" * 90, "c" * 90, "d" * 90])
        )
        related_directory.mkdir(parents=True)
        for index in range(20):
            page_id = uuid4()
            title = f"Related page {index} " + ("x" * 180)
            markdown = (
                f"---\nkg_id: {page_id}\nkg_kind: wiki\nkg_status: reviewed\n"
                f"title: {json.dumps(title, ensure_ascii=False)}\n---\n\nExisting notes.\n"
            )
            (related_directory / f"{page_id}.md").write_text(markdown, encoding="utf-8")

        catalog = await self.wiki.list_pages(limit=20)
        self.assertEqual(len(catalog["pages"]), 20)
        await self.service._process(self.generation_repository.job())

        related_pages = self.generation_repository.result["model"]["related_pages"]
        serialized = json.dumps(related_pages, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(related_pages), 8)
        self.assertLessEqual(len(serialized), 4096)
        self.assertGreater(len(related_pages), 0)
        self.assertIsNotNone(self.generation_repository.completed)
        self.assertEqual(self.core.generate_calls, 1)


if __name__ == "__main__":
    unittest.main()
