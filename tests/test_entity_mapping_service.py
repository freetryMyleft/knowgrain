from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import unittest
from types import SimpleNamespace
from uuid import uuid4

from knowgrain.entity_mapping_service import (
    EntityMappingService,
    EntityMappingValidationError,
)
from knowgrain.m3_types import Evidence


def make_evidence(chunk_id: str = "chunk-1") -> Evidence:
    excerpt = "verified quote"
    return Evidence(
        evidence_id=uuid4(),
        source_id=uuid4(),
        revision_id=uuid4(),
        filename="source.txt",
        vault_path="Sources/source.txt",
        source_sha256="a" * 64,
        parsed_text_sha256="b" * 64,
        chunk_id=chunk_id,
        excerpt=excerpt,
        excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        start=0,
        end=len(excerpt),
        page=None,
        heading=None,
        indexed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


class MappingWiki:
    def __init__(self, page_id, sha: str):
        self.page_id = page_id
        self.sha = sha
        self.get_calls = 0

    async def get_page(self, page_id):
        if page_id != self.page_id:
            raise LookupError("missing")
        self.get_calls += 1
        return {
            "page_id": str(page_id),
            "content_sha256": self.sha,
            "title": "Current title",
            "vault_path": "Wiki/Pages/current.md",
        }


class MappingGeneration:
    def __init__(self, page_id, item: Evidence, sha: str):
        self.page_id = page_id
        self.item = item
        self.sha = sha
        self.review_repository = SimpleNamespace(get_binding=self.get_binding)
        self.repository = SimpleNamespace(get_generation=self.get_generation)
        self.detail_calls = 0
        self.detail_sha_override = None

    async def get_binding(self, page_id):
        return None

    async def get_generation(self, page_id):
        if page_id != self.page_id:
            return None
        return {"generation_page_id": str(page_id)}

    async def generation_detail(self, page_id):
        self.detail_calls += 1
        sha = self.detail_sha_override or self.sha
        return {
            "page_id": str(page_id),
            "generation_page_id": str(page_id),
            "current_sha256": sha,
            "content_modified": False,
            "evidence_current": True,
            "evidence": [self.item.snapshot()],
            "vault_path": "Wiki/Pages/current.md",
            # Deliberately no title: GenerationService.generation_detail has none.
        }


class MappingCore:
    def __init__(self, item: Evidence):
        self.item = item
        self.chunk_ids = (item.chunk_id,)
        self.forward_result = {
            "entities": [{
                "entity_id": hashlib.sha256("Café.Entity".encode()).hexdigest(),
                "name": "Café.Entity",
                "entity_type": "organization",
                "evidence_ids": [str(item.evidence_id)],
            }],
            "truncated": False,
        }
        self.forward_calls = []

    async def entity_chunk_ids(self, name):
        return self.chunk_ids

    async def entities_for_evidence(self, evidence):
        self.forward_calls.append(tuple(evidence))
        return self.forward_result


class EntityMappingServiceTests(unittest.IsolatedAsyncioTestCase):
    def build_service(self):
        page_id = uuid4()
        item = make_evidence()
        sha = "c" * 64
        wiki = MappingWiki(page_id, sha)
        generation = MappingGeneration(page_id, item, sha)
        core = MappingCore(item)
        repository = SimpleNamespace(
            candidate_page_ids=lambda chunk_ids, *, limit: self._candidate(page_id, chunk_ids, limit)
        )
        return (
            EntityMappingService(generation, repository, core, wiki),
            page_id,
            item,
            wiki,
            generation,
            core,
        )

    async def _candidate(self, page_id, chunk_ids, limit):
        self.assertEqual(chunk_ids, ("chunk-1",))
        self.assertEqual(limit, 50)
        return {"page_ids": (page_id,), "truncated": False}

    async def test_manual_page_returns_empty_mapping_without_core_call(self):
        service, page_id, _item, _wiki, generation, core = self.build_service()
        generation.repository.get_generation = lambda _page_id: _none()

        result = await service.page_entities(page_id)

        self.assertFalse(result["binding_current"])
        self.assertFalse(result["evidence_current"])
        self.assertEqual(result["entities"], [])
        self.assertEqual(core.forward_calls, [])

    async def test_page_mapping_accepts_dotted_key_and_rechecks_wiki_hash(self):
        service, page_id, item, wiki, _generation, core = self.build_service()

        result = await service.page_entities(page_id)

        self.assertEqual(result["entities"][0]["name"], "Café.Entity")
        self.assertEqual(result["entities"][0]["evidence_ids"], [str(item.evidence_id)])
        self.assertEqual(wiki.get_calls, 2)

    async def test_page_mapping_drops_result_when_file_changes_after_core_read(self):
        service, page_id, _item, wiki, _generation, core = self.build_service()

        async def mutate_after_read(_evidence):
            wiki.sha = "d" * 64
            return core.forward_result

        core.entities_for_evidence = mutate_after_read
        result = await service.page_entities(page_id)

        self.assertEqual(result["entities"], [])
        self.assertFalse(result["binding_current"])
        self.assertEqual(result["content_sha256"], "d" * 64)

    async def test_reverse_mapping_uses_wiki_title_without_generation_title_field(self):
        service, page_id, item, _wiki, generation, _core = self.build_service()

        result = await service.entity_pages("Café.Entity")

        self.assertEqual(result["pages"], [{
            "page_id": str(page_id),
            "title": "Current title",
            "vault_path": "Wiki/Pages/current.md",
            "content_sha256": "c" * 64,
            "evidence_ids": [str(item.evidence_id)],
        }])
        self.assertEqual(generation.detail_calls, 2)

    async def test_reverse_mapping_preserves_core_truncation_when_no_match_is_returned(self):
        service, _page_id, _item, _wiki, _generation, core = self.build_service()
        core.forward_result = {"entities": [], "truncated": True}

        result = await service.entity_pages("Café.Entity")

        self.assertEqual(result["pages"], [])
        self.assertTrue(result["truncated"])

    async def test_reverse_mapping_rechecks_file_hash_before_returning_page(self):
        service, _page_id, _item, wiki, _generation, core = self.build_service()

        async def mutate_after_read(evidence):
            result = core.forward_result
            wiki.sha = "e" * 64
            return result

        core.entities_for_evidence = mutate_after_read
        result = await service.entity_pages("Café.Entity")

        self.assertEqual(result["pages"], [])

    async def test_name_matches_core_unicode_validation_and_bounds(self):
        for invalid in ("\u200b", "line\u2028break", "bad\x00name", "x" * 513):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(
                EntityMappingValidationError
            ):
                EntityMappingService.validate_name(invalid)
        self.assertEqual(EntityMappingService.validate_name("https://example.test/a"), "https://example.test/a")


async def _none():
    return None


if __name__ == "__main__":
    unittest.main()
