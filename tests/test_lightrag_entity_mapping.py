"""Revision-safe entity mapping against LightRAG's storage interfaces."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from knowgrain.config import Settings
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.m3_types import Evidence


def evidence(*, chunk_id: str, revision_id, excerpt: str = "verified quote") -> Evidence:
    return Evidence(
        evidence_id=uuid4(),
        source_id=uuid4(),
        revision_id=revision_id,
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


def runtime_with_storages(*, text_chunks, full_entities, entity_chunks, graph):
    if isinstance(text_chunks, AsyncMock):
        text_chunks = SimpleNamespace(get_by_ids=text_chunks)
    if isinstance(full_entities, AsyncMock):
        full_entities = SimpleNamespace(get_by_ids=full_entities)
    if isinstance(entity_chunks, AsyncMock):
        entity_chunks = SimpleNamespace(get_by_ids=entity_chunks)
    runtime = LightRAGRuntime(Settings(_env_file=None))
    runtime._rag = SimpleNamespace(
        text_chunks=text_chunks,
        full_entities=full_entities,
        entity_chunks=entity_chunks,
        chunk_entity_relation_graph=graph,
    )
    return runtime


class EntityMappingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_only_verified_current_chunks_and_ignores_graph_source_id(self):
        current_revision = uuid4()
        unrelated_revision = uuid4()
        current = evidence(chunk_id="chunk-current", revision_id=current_revision)
        stale = evidence(chunk_id="chunk-wrong-revision", revision_id=current_revision)
        absent = evidence(chunk_id="chunk-missing", revision_id=current_revision)
        quote_mismatch = evidence(
            chunk_id="chunk-quote-mismatch", revision_id=current_revision,
            excerpt="quote that is no longer present",
        )

        text_records = {
            "chunk-current": {"full_doc_id": str(current_revision), "content": "verified quote plus text"},
            # This candidate belongs to the same evidence object at the API edge,
            # but Core says it is a different source revision.
            "chunk-wrong-revision": {"full_doc_id": str(unrelated_revision), "content": "verified quote plus text"},
            "chunk-missing": None,
            "chunk-quote-mismatch": {"full_doc_id": str(current_revision), "content": "replacement quote"},
        }
        text_chunks = AsyncMock(side_effect=lambda ids: [text_records[item] for item in ids])
        full_entities = AsyncMock(return_value=[
            {"entity_names": ["Café", "Café"], "count": 2},
        ])
        entity_chunks = AsyncMock(return_value=[{
            "chunk_ids": ["chunk-current", "chunk-old-unrelated"],
            "count": 2,
        }])
        graph = SimpleNamespace(get_nodes_batch=AsyncMock(return_value={
            "Café": {
                "entity_name": "Café",
                "entity_type": "organization",
                # A truncated source field must never influence citations.
                "source_id": "chunk-wrong-revision<SEP>chunk-old-unrelated",
            },
        }))
        runtime = runtime_with_storages(
            text_chunks=text_chunks,
            full_entities=full_entities,
            entity_chunks=entity_chunks,
            graph=graph,
        )

        result = await runtime.entities_for_evidence([current, stale, absent, quote_mismatch])

        self.assertEqual(result["truncated"], False)
        self.assertEqual(result["entities"], [{
            "entity_id": hashlib.sha256("Café".encode()).hexdigest(),
            "name": "Café",
            "entity_type": "organization",
            "evidence_ids": [str(current.evidence_id)],
        }])
        text_chunks.assert_awaited_once_with([
            "chunk-current", "chunk-missing", "chunk-quote-mismatch", "chunk-wrong-revision",
        ])
        # The mismatched and missing chunks do not authorize reading their documents.
        full_entities.assert_awaited_once_with([str(current_revision)])
        entity_chunks.assert_awaited_once_with(["Café"])
        graph.get_nodes_batch.assert_awaited_once_with(["Café"])

    async def test_excludes_missing_membership_or_graph_node_and_defaults_missing_type(self):
        revision = uuid4()
        item = evidence(chunk_id="chunk-1", revision_id=revision)
        text_chunks = AsyncMock(return_value=[{
            "full_doc_id": str(revision), "content": "verified quote follows",
        }])
        full_entities = AsyncMock(return_value=[{
            "entity_names": ["MissingMembership", "MissingNode", "Untyped"],
            "count": 3,
        }])
        entity_chunks = AsyncMock(return_value=[
            None,
            {"chunk_ids": ["chunk-1"], "count": 1},
            {"chunk_ids": ["chunk-1"], "count": 1},
        ])
        graph = SimpleNamespace(get_nodes_batch=AsyncMock(return_value={
            "Untyped": {"entity_name": "Untyped"},
        }))
        runtime = runtime_with_storages(
            text_chunks=text_chunks,
            full_entities=full_entities,
            entity_chunks=entity_chunks,
            graph=graph,
        )

        result = await runtime.entities_for_evidence([item])

        self.assertEqual([entry["name"] for entry in result["entities"]], ["Untyped"])
        self.assertEqual(result["entities"][0]["entity_type"], "UNKNOWN")
        self.assertEqual(result["entities"][0]["evidence_ids"], [str(item.evidence_id)])

    async def test_caps_names_and_output_with_truncation_flag(self):
        revision = uuid4()
        item = evidence(chunk_id="chunk-1", revision_id=revision)
        names = [f"Entity-{index:03d}" for index in range(505)]
        text_chunks = AsyncMock(return_value=[{
            "full_doc_id": str(revision), "content": "verified quote follows",
        }])
        full_entities = AsyncMock(return_value=[{"entity_names": names, "count": len(names)}])
        entity_chunks = AsyncMock(return_value=[
            {"chunk_ids": ["chunk-1"], "count": 1} for _ in range(500)
        ])
        graph = SimpleNamespace(get_nodes_batch=AsyncMock(return_value={
            name: {"entity_name": name, "entity_type": "concept"}
            for name in names[:500]
        }))
        runtime = runtime_with_storages(
            text_chunks=text_chunks,
            full_entities=full_entities,
            entity_chunks=entity_chunks,
            graph=graph,
        )

        result = await runtime.entities_for_evidence([item])

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["entities"]), 100)
        self.assertEqual(
            [entry["name"] for entry in result["entities"]],
            sorted(names)[:100],
        )
        entity_chunks.assert_awaited_once_with(sorted(names)[:500])
        graph.get_nodes_batch.assert_awaited_once_with(sorted(names)[:500])

    async def test_forward_mapping_fails_closed_on_oversized_membership(self):
        revision = uuid4()
        item = evidence(chunk_id="chunk-1", revision_id=revision)
        runtime = runtime_with_storages(
            text_chunks=AsyncMock(return_value=[{
                "full_doc_id": str(revision), "content": "verified quote follows",
            }]),
            full_entities=AsyncMock(return_value=[{
                "entity_names": ["X"], "count": 1,
            }]),
            entity_chunks=AsyncMock(return_value=[{
                "chunk_ids": [f"chunk-{index}" for index in range(10_001)],
                "count": 10_001,
            }]),
            graph=SimpleNamespace(get_nodes_batch=AsyncMock()),
        )

        with self.assertRaisesRegex(RuntimeError, "exceeds the supported limit"):
            await runtime.entities_for_evidence([item])

        runtime._rag.chunk_entity_relation_graph.get_nodes_batch.assert_not_awaited()

    async def test_entity_chunk_ids_checks_graph_and_reads_exact_name_key(self):
        chunks = AsyncMock(return_value={"chunk_ids": ["c2", "c1"], "count": 2})
        graph = SimpleNamespace(get_nodes_batch=AsyncMock(return_value={
            "Exact Name": {"entity_name": "Exact Name"},
        }))
        runtime = runtime_with_storages(
            text_chunks=AsyncMock(),
            full_entities=AsyncMock(),
            entity_chunks=SimpleNamespace(get_by_id=chunks),
            graph=graph,
        )

        self.assertEqual(await runtime.entity_chunk_ids("Exact Name"), ("c1", "c2"))
        graph.get_nodes_batch.assert_awaited_once_with(["Exact Name"])
        chunks.assert_awaited_once_with("Exact Name")

        graph.get_nodes_batch.reset_mock(return_value=True)
        graph.get_nodes_batch.return_value = {}
        chunks.reset_mock()
        self.assertEqual(await runtime.entity_chunk_ids("Unmapped"), ())
        chunks.assert_not_awaited()

    async def test_entity_chunk_ids_fails_closed_above_bound(self):
        graph = SimpleNamespace(get_nodes_batch=AsyncMock(return_value={"X": {}}))
        membership = AsyncMock(return_value={
            "chunk_ids": [f"chunk-{index}" for index in range(10_001)],
            "count": 10_001,
        })
        runtime = runtime_with_storages(
            text_chunks=AsyncMock(),
            full_entities=AsyncMock(),
            entity_chunks=SimpleNamespace(get_by_id=membership),
            graph=graph,
        )

        with self.assertRaisesRegex(RuntimeError, "exceeds the supported limit"):
            await runtime.entity_chunk_ids("X")

    async def test_mapping_rejects_use_from_another_event_loop_before_storage_access(self):
        runtime = runtime_with_storages(
            text_chunks=AsyncMock(),
            full_entities=AsyncMock(),
            entity_chunks=AsyncMock(),
            graph=SimpleNamespace(get_nodes_batch=AsyncMock()),
        )
        other_loop = asyncio.new_event_loop()
        runtime._event_loop = other_loop
        try:
            with self.assertRaisesRegex(RuntimeError, "initialization event loop"):
                await runtime.entities_for_evidence([])
            runtime._rag.text_chunks.get_by_ids.assert_not_awaited()
        finally:
            other_loop.close()


if __name__ == "__main__":
    unittest.main()
