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
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
from sqlalchemy.exc import SQLAlchemyError

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
from knowgrain.wiki_files import WikiConflictError, _frontmatter, parse_wiki
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
        self.fail_projection_after_successes = None

    async def replace_projection(self, pages) -> None:
        if self.fail_projection_after_successes is not None:
            if self.fail_projection_after_successes == 0:
                self.fail_projection_after_successes = None
                raise SQLAlchemyError("simulated projection interruption")
            self.fail_projection_after_successes -= 1
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
        self.target_page_id = None
        self.expected_target_sha256 = None

    def job(self):
        return {
            "job_id": str(self.job_id),
            "output_page_id": str(self.output_page_id),
            "topic": "A source-backed topic",
            "target_page_id": str(self.target_page_id) if self.target_page_id else None,
            "expected_target_sha256": self.expected_target_sha256,
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
            "proposal_target_page_id": str(self.target_page_id) if self.target_page_id else None,
            "proposal_target_sha256": self.expected_target_sha256,
            "reviewed_sha256": None,
            "reviewed_at": None,
            "created_at": "2026-10-01T00:00:00+00:00",
        }

    async def get_evidence(self, evidence_id):
        return self.stored_evidence.get(evidence_id)


class InMemoryReviewRepository:
    """Review ledger test double; file intents remain real and journaled."""

    def __init__(self, wiki_repository: InMemoryWikiRepository) -> None:
        self.wiki_repository = wiki_repository
        self.operations = {}
        self.bindings = {}
        self.fail_complete_once = False

    async def get_binding(self, page_id):
        return self.bindings.get(page_id)

    async def prepare(self, operation_id, **fields):
        snapshot = {**fields, "state": "prepared"}
        current = self.operations.get(operation_id)
        if current and any(current.get(key) != value for key, value in fields.items()):
            from knowgrain.generation_repository import GenerationConflictError

            raise GenerationConflictError("operation mismatch")
        if current is None:
            self.operations[operation_id] = snapshot
        return self.operations[operation_id]

    async def complete(self, operation_id):
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise SQLAlchemyError("simulated database completion interruption")
        operation = self.operations[operation_id]
        operation["state"] = "completed"
        self.bindings[operation["page_id"]] = {
            "generation_page_id": str(operation["generation_page_id"]),
            "reviewed_sha256": operation["reviewed_sha256"],
            "reviewed_at": "2026-10-01T00:00:00+00:00",
            "operation_id": str(operation_id),
        }
        return operation


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
        self.review_repository = InMemoryReviewRepository(self.wiki_repository)
        self.service = GenerationService(
            Settings(_env_file=None),
            self.generation_repository,
            self.provenance,
            self.core,
            self.wiki,
            review_repository=self.review_repository,
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

    async def test_explicit_review_moves_draft_and_retry_keeps_binding(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        generated = await self.wiki.get_page(page_id)

        reviewed = await self.service.review_page(page_id, generated["content_sha256"])
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["vault_path"], f"Wiki/Pages/{page_id}.md")
        self.assertEqual(
            self.vault.resolve(reviewed["vault_path"]).read_text(encoding="utf-8"),
            reviewed["markdown"],
        )
        binding = await self.review_repository.get_binding(page_id)
        self.assertEqual(binding["generation_page_id"], str(page_id))
        self.assertEqual(binding["reviewed_sha256"], reviewed["content_sha256"])

        retry = await self.service.review_page(page_id, generated["content_sha256"])
        self.assertEqual(retry["content_sha256"], reviewed["content_sha256"])
        self.assertEqual(len(self.review_repository.operations), 1)

        detail = await self.service.generation_detail(page_id)
        self.assertEqual(detail["page_id"], str(page_id))
        self.assertEqual(detail["generation_page_id"], str(page_id))
        self.assertIsNone(detail["proposal"])
        self.assertFalse(detail["content_modified"])

    async def test_review_retries_after_projection_failure_without_overwriting(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        generated = await self.wiki.get_page(page_id)
        # _review_scan_locked first projects the old state; fail the next
        # replacement, which happens after the journaled file commit.
        self.wiki_repository.fail_projection_after_successes = 1
        with self.assertRaisesRegex(SQLAlchemyError, "simulated projection interruption"):
            await self.service.review_page(page_id, generated["content_sha256"])
        reviewed_path = self.vault.resolve(f"Wiki/Pages/{page_id}.md")
        draft_path = self.vault.resolve(f"Wiki/Drafts/{page_id}.md")
        self.assertTrue(reviewed_path.exists())
        self.assertFalse(draft_path.exists())

        retried = await self.service.review_page(page_id, generated["content_sha256"])
        self.assertEqual(retried["status"], "reviewed")
        self.assertEqual(len(self.review_repository.operations), 1)

    async def test_review_api_returns_503_after_completion_failure_then_retry_succeeds(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        generated = await self.wiki.get_page(page_id)
        self.review_repository.fail_complete_once = True
        app = self._generation_app()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://knowgrain.test"
        ) as client:
            first = await client.post(
                f"/api/v1/wiki/pages/{page_id}/review",
                json={"expected_sha256": generated["content_sha256"]},
            )
            retry = await client.post(
                f"/api/v1/wiki/pages/{page_id}/review",
                json={"expected_sha256": generated["content_sha256"]},
            )
            stale = await client.post(
                f"/api/v1/wiki/pages/{page_id}/review",
                json={"expected_sha256": "0" * 64},
            )

        self.assertEqual(first.status_code, 503)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["status"], "reviewed")
        self.assertEqual(stale.status_code, 409)

    async def test_review_api_recovers_partial_move_after_duplicate_scan_projection(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        generated = await self.wiki.get_page(page_id)
        app = self._generation_app()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://knowgrain.test"
        ) as client:
            with patch.object(
                self.service.review_files,
                "_remove_old_after_move",
                side_effect=OSError("simulated interruption after destination publication"),
            ):
                first = await client.post(
                    f"/api/v1/wiki/pages/{page_id}/review",
                    json={"expected_sha256": generated["content_sha256"]},
                )
            # Simulate the background watcher reconciling the duplicate-ID scan
            # before the user's explicit same-request retry arrives.
            await self.wiki._scan_locked()
            retry = await client.post(
                f"/api/v1/wiki/pages/{page_id}/review",
                json={"expected_sha256": generated["content_sha256"]},
            )

        self.assertEqual(first.status_code, 503)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["status"], "reviewed")
        self.assertFalse(self.vault.resolve(f"Wiki/Drafts/{page_id}.md").exists())
        self.assertTrue(self.vault.resolve(f"Wiki/Pages/{page_id}.md").is_file())

    async def test_unjournaled_duplicate_does_not_repair_projection_or_prepare_review(self):
        await self.service._process(self.generation_repository.job())
        page_id = self.generation_repository.output_page_id
        generated = await self.wiki.get_page(page_id)
        metadata, body = _frontmatter(generated["markdown"])
        duplicate_markdown = self.service._render_reviewed_markdown(
            {**metadata, "kg_id": str(page_id), "kg_status": "reviewed"}, body
        )
        duplicate_path = f"Wiki/Pages/{page_id}.md"
        self.wiki.files._ensure_parent(duplicate_path)
        self.wiki.files._publish_exclusive(
            self.vault.resolve(duplicate_path), duplicate_markdown.encode("utf-8")
        )
        # Reproduce a prior watcher pass: a duplicate scan makes the projected
        # identity unavailable before the explicit review request arrives.
        await self.wiki._scan_locked()
        self.assertNotIn(page_id, self.wiki_repository.pages)

        with self.assertRaises(WikiConflictError):
            await self.service.review_page(page_id, generated["content_sha256"])

        self.assertNotIn(page_id, self.wiki_repository.pages)
        self.assertEqual(self.review_repository.operations, {})
        self.assertTrue(self.vault.resolve(f"Wiki/Drafts/{page_id}.md").is_file())
        self.assertTrue(self.vault.resolve(duplicate_path).is_file())

    async def test_proposal_apply_preserves_target_metadata_and_manifest_binding(self):
        target = await self.wiki.create("Human-owned title", "Human-owned notes.\n")
        target_id = UUID(target["page_id"])
        self.generation_repository.target_page_id = target_id
        self.generation_repository.expected_target_sha256 = target["content_sha256"]
        await self.service._process(self.generation_repository.job())
        proposal_id = self.generation_repository.output_page_id
        proposal = await self.wiki.get_page(proposal_id)

        detail = await self.service.generation_detail(proposal_id)
        self.assertEqual(detail["generation_page_id"], str(proposal_id))
        self.assertIsInstance(detail["proposal"], dict)
        proposal_diff = detail["proposal"]["diff"]
        self.assertLessEqual(len(proposal_diff.encode("utf-8")), 64 * 1024)
        self.assertIn("Human-owned notes.", proposal_diff)
        self.assertIn("The source records a verified fact", proposal_diff)
        self.assertNotIn("kg_id", proposal_diff)
        self.assertNotIn("kg_status", proposal_diff)

        applied = await self.service.apply_proposal(
            proposal_id,
            expected_proposal_sha256=proposal["content_sha256"],
            expected_target_sha256=target["content_sha256"],
        )
        self.assertEqual(applied["page_id"], str(target_id))
        self.assertEqual(applied["status"], "reviewed")
        self.assertEqual(applied["title"], "Human-owned title")
        self.assertIn("The source records a verified fact", applied["markdown"])
        self.assertEqual(
            applied["vault_path"], f"Wiki/Pages/{target_id}.md"
        )
        # Applying changes the current binding only; proposal and its manifest stay intact.
        self.assertTrue(self.vault.resolve(f"Wiki/Drafts/{proposal_id}.md").exists())
        self.assertEqual(
            (await self.service.generation_detail(target_id))["generation_page_id"],
            str(proposal_id),
        )
        proposal_detail = await self.service.generation_detail(proposal_id)
        self.assertEqual(proposal_detail["generated_sha256"], proposal["content_sha256"])

        retry = await self.service.apply_proposal(
            proposal_id,
            expected_proposal_sha256=proposal["content_sha256"],
            expected_target_sha256=target["content_sha256"],
        )
        self.assertEqual(retry["content_sha256"], applied["content_sha256"])
        self.assertEqual(len(self.review_repository.operations), 1)

    async def test_proposal_target_or_evidence_drift_blocks_application(self):
        target = await self.wiki.create("Target title", "Original target.\n")
        target_id = UUID(target["page_id"])
        self.generation_repository.target_page_id = target_id
        self.generation_repository.expected_target_sha256 = target["content_sha256"]
        await self.service._process(self.generation_repository.job())
        proposal_id = self.generation_repository.output_page_id
        proposal = await self.wiki.get_page(proposal_id)

        edited_target = target["markdown"] + "External edit.\n"
        self.vault.resolve(target["vault_path"]).write_text(edited_target, encoding="utf-8")
        with self.assertRaises(WikiConflictError):
            await self.service.apply_proposal(
                proposal_id,
                expected_proposal_sha256=proposal["content_sha256"],
                expected_target_sha256=target["content_sha256"],
            )
        self.assertEqual(
            self.vault.resolve(target["vault_path"]).read_text(encoding="utf-8"),
            edited_target,
        )

        # Restore the exact target snapshot, then make the evidence source stale.
        self.vault.resolve(target["vault_path"]).write_text(target["markdown"], encoding="utf-8")
        self.source_path.write_bytes(b"Source changed before explicit application.")
        with self.assertRaises(EvidenceUnavailableError):
            await self.service.apply_proposal(
                proposal_id,
                expected_proposal_sha256=proposal["content_sha256"],
                expected_target_sha256=target["content_sha256"],
            )
        self.assertEqual(
            self.vault.resolve(target["vault_path"]).read_text(encoding="utf-8"),
            target["markdown"],
        )

    def _generation_app(self):
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
        return app


if __name__ == "__main__":
    unittest.main()
