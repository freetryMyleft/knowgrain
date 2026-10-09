"""E3 observation checks with fixed synthetic content and no model requests."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import test_core_content_manifest as e2

from knowgrain.core_audit import (
    AuditError,
    audit_persisted_content,
    audit_persisted_workspace_content,
)
from knowgrain.core_preflight import ResolvedPostgresConfig
from knowgrain.index_identity import CoreIndexIdentity


class AuditTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        e2.ContentManifestTests.setUpClass()
        cls.addClassCleanup(e2.ContentManifestTests.doClassCleanups)
        cls.builder = e2.ContentManifestTests()
        cls.tokenizer = e2.ContentManifestTests.tokenizer
        cls.cache = e2.ContentManifestTests.cache
        cls.manifest = cls.builder.build(b"synthetic audit text")

    def setUp(self):
        self.identity = CoreIndexIdentity("audit", Path("/tmp/audit"), "kg_" + "a" * 24)
        self.config = ResolvedPostgresConfig("localhost", 55434, "test", "test", ssl_mode="disable")
        from knowgrain.core_audit import _vector_table_names

        self.names = _vector_table_names(self.identity, 2)
        m = self.manifest
        self.rows = {
            name: []
            for name in (
                "lightrag_doc_full",
                "lightrag_doc_status",
                "lightrag_doc_chunks",
                "lightrag_full_entities",
                "lightrag_full_relations",
                "lightrag_entity_chunks",
                "lightrag_relation_chunks",
                "lightrag_graph_nodes",
                "lightrag_graph_edges",
                "lightrag_llm_cache",
                *self.names.values(),
            )
        }
        self.rows["lightrag_doc_full"] = [
            {
                "id": str(m.revision_id),
                "content": m.core_text,
                "doc_name": m.canonical_file_path,
                "content_hash": m.core_sha256,
                "parse_format": m.raw_format,
                "process_options": None,
                "chunk_options": json.loads(m.chunk_options),
                "parse_engine": None,
                "meta": None,
                "sidecar_location": None,
            }
        ]
        self.rows["lightrag_doc_status"] = [
            {
                "id": str(m.revision_id),
                "status": "processed",
                "content_hash": m.core_sha256,
                "content_length": len(m.core_text),
                "chunks_count": len(m.chunks),
                "chunks_list": [c.id for c in m.chunks],
                "file_path": m.canonical_file_path,
                "metadata": {"parse_format": m.raw_format},
                "error_msg": None,
            }
        ]
        self.rows["lightrag_doc_chunks"] = [
            {
                "id": c.id,
                "full_doc_id": c.full_doc_id,
                "chunk_order_index": c.chunk_order_index,
                "tokens": c.tokens,
                "content": c.content,
                "file_path": c.file_path,
                "llm_cache_list": [],
                "heading": {},
                "sidecar": {},
            }
            for c in m.chunks
        ]
        self.rows[self.names["chunks"]] = [
            dict(r, vector_text="[1,2]") for r in self.rows["lightrag_doc_chunks"]
        ]
        self.rows["lightrag_full_entities"] = [
            {"id": str(m.revision_id), "entity_names": [], "count": 0}
        ]
        self.rows["lightrag_full_relations"] = [
            {"id": str(m.revision_id), "relation_pairs": [], "count": 0}
        ]

    def graph(self):
        chunks = [c.id for c in self.manifest.chunks]
        self.rows["lightrag_full_entities"][0].update(entity_names=["A", "B"], count=2)
        self.rows["lightrag_full_relations"][0].update(relation_pairs=[["A", "B"]], count=1)
        for name in ("A", "B"):
            props = {
                "entity_id": name,
                "entity_type": "concept",
                "description": "synthetic description",
                "source_id": "<SEP>".join(chunks),
                "file_path": self.manifest.canonical_file_path,
            }
            self.rows["lightrag_graph_nodes"].append(
                {"id": name, "namespace": "chunk_entity_relation", "properties": props}
            )
            self.rows["lightrag_entity_chunks"].append(
                {"id": name, "chunk_ids": chunks, "count": len(chunks)}
            )
            self.rows[self.names["entity"]].append(
                {
                    "id": "ent-" + hashlib.md5(name.encode()).hexdigest(),
                    "entity_name": name,
                    "content": name + "\n" + props["description"],
                    "file_path": props["file_path"],
                    "chunk_ids": chunks,
                    "vector_text": "[1,2]",
                }
            )
        props = {
            "description": "synthetic relation",
            "keywords": "related",
            "weight": 1.0,
            "source_id": "<SEP>".join(chunks),
            "file_path": self.manifest.canonical_file_path,
        }
        self.rows["lightrag_graph_edges"] = [
            {
                "src_id": "A",
                "tgt_id": "B",
                "namespace": "chunk_entity_relation",
                "properties": props,
            }
        ]
        self.rows["lightrag_relation_chunks"] = [
            {"id": "A<SEP>B", "chunk_ids": chunks, "count": len(chunks)}
        ]
        self.rows[self.names["relation"]] = [
            {
                "id": "rel-" + hashlib.md5(b"AB").hexdigest(),
                "source_id": "A",
                "target_id": "B",
                "content": "related\tA\nB\nsynthetic relation",
                "chunk_ids": chunks,
                "file_path": props["file_path"],
                "vector_text": "[1,2]",
            }
        ]

    async def audit(self, workspace=True):
        with patch("knowgrain.core_audit._audit_connection", AsyncMock(return_value=self.rows)):
            if workspace:
                return await audit_persisted_workspace_content(
                    [self.manifest],
                    self.config,
                    self.identity,
                    tokenizer=self.tokenizer,
                    tokenizer_cache_dir=self.cache,
                )
            return await audit_persisted_content(
                self.manifest,
                self.config,
                self.identity,
                tokenizer=self.tokenizer,
                tokenizer_cache_dir=self.cache,
            )

    async def test_empty_graph_success(self):
        self.assertTrue((await self.audit()).passed)
        self.assertTrue((await self.audit(False)).passed)

    async def test_graph_success(self):
        self.graph()
        self.assertTrue((await self.audit()).passed)

    async def test_document_status_and_metadata_corruption(self):
        cases = [
            ("lightrag_doc_full", "content", "corrupted"),
            ("lightrag_doc_full", "content_hash", "bad"),
            ("lightrag_doc_full", "chunk_options", {}),
            ("lightrag_doc_full", "parse_engine", "custom"),
            ("lightrag_doc_full", "doc_name", "wrong"),
            ("lightrag_doc_status", "status", "failed"),
            ("lightrag_doc_status", "content_length", 999),
            ("lightrag_doc_status", "chunks_count", 99),
            ("lightrag_doc_status", "chunks_list", []),
            ("lightrag_doc_status", "metadata", {}),
        ]
        original = copy.deepcopy(self.rows)
        for table, field, value in cases:
            with self.subTest(table=table, field=field):
                self.rows = copy.deepcopy(original)
                self.rows[table][0][field] = value
                self.assertFalse((await self.audit()).passed)

    async def test_missing_and_extra_surface_records(self):
        original = copy.deepcopy(self.rows)
        for table in (
            "lightrag_doc_full",
            "lightrag_doc_status",
            "lightrag_doc_chunks",
            "lightrag_full_entities",
            "lightrag_full_relations",
            self.names["chunks"],
        ):
            for kind in ("missing", "extra"):
                with self.subTest(table=table, kind=kind):
                    self.rows = copy.deepcopy(original)
                    if kind == "missing":
                        self.rows[table] = []
                    else:
                        self.rows[table].append(dict(self.rows[table][0], id="unexpected"))
                    self.assertFalse((await self.audit()).passed)

    async def test_chunk_and_vector_corruption(self):
        original = copy.deepcopy(self.rows)
        for table, key, value in [
            ("lightrag_doc_chunks", "content", "wrong"),
            ("lightrag_doc_chunks", "tokens", 999),
            ("lightrag_doc_chunks", "chunk_order_index", 2),
            ("lightrag_doc_chunks", "full_doc_id", "orphan"),
            ("lightrag_doc_chunks", "llm_cache_list", ["missing-cache"]),
            (self.names["chunks"], "vector_text", None),
            (self.names["chunks"], "vector_text", "[1]"),
            (self.names["chunks"], "vector_text", "[nan,2]"),
            (self.names["chunks"], "vector_text", "[inf,2]"),
            (self.names["chunks"], "vector_text", "invalid"),
            (self.names["chunks"], "content", "wrong"),
        ]:
            with self.subTest(table=table, key=key):
                self.rows = copy.deepcopy(original)
                self.rows[table][0][key] = value
                self.assertFalse((await self.audit()).passed)

    async def test_runtime_cache_list_not_required_empty(self):
        chunk = self.manifest.chunks[0].id
        self.rows["lightrag_llm_cache"] = [
            {"id": "extract:synthetic", "chunk_id": chunk, "cache_type": "extract"}
        ]
        self.rows["lightrag_doc_chunks"][0]["llm_cache_list"] = ["extract:synthetic"]
        self.assertTrue((await self.audit()).passed)

    async def test_graph_corruption(self):
        self.graph()
        original = copy.deepcopy(self.rows)
        cases = [
            ("lightrag_graph_nodes", "namespace", "unexpected"),
            ("lightrag_graph_nodes", "properties", {}),
            ("lightrag_graph_edges", "tgt_id", "missing"),
            ("lightrag_entity_chunks", "chunk_ids", ["missing"]),
            ("lightrag_relation_chunks", "id", "A->B"),
            ("lightrag_relation_chunks", "count", 99),
            (self.names["entity"], "content", "wrong"),
            (self.names["entity"], "id", "wrong"),
            (self.names["entity"], "chunk_ids", []),
            (self.names["relation"], "content", "wrong"),
            (self.names["relation"], "source_id", "missing"),
            (self.names["relation"], "vector_text", "[1,nan]"),
        ]
        for table, key, value in cases:
            with self.subTest(table=table, key=key):
                self.rows = copy.deepcopy(original)
                self.rows[table][0][key] = value
                result = await self.audit()
                self.assertFalse(result.passed)
                self.assertNotIn("synthetic description", repr(result))
                self.assertNotIn("synthetic audit text", repr(result))

    def append_incremental_source_with_old_paths(self):
        self.graph()
        second = self.builder.build(
            b"second synthetic source", revision_id=UUID(int=2), vault_path="sources/second.txt"
        )
        fixture = AuditTests()
        fixture.manifest = second
        fixture.setUp()
        fixture.graph()
        for table in (
            "lightrag_doc_full",
            "lightrag_doc_status",
            "lightrag_doc_chunks",
            "lightrag_full_entities",
            "lightrag_full_relations",
            self.names["chunks"],
        ):
            self.rows[table].extend(fixture.rows[table])
        ids = [c.id for m in (self.manifest, second) for c in m.chunks]
        for table in ("lightrag_entity_chunks", "lightrag_relation_chunks"):
            for row in self.rows[table]:
                row.update(chunk_ids=ids, count=len(ids))
        for table in ("lightrag_graph_nodes", "lightrag_graph_edges"):
            for row in self.rows[table]:
                row["properties"]["source_id"] = "<SEP>".join(ids)
        for kind in ("entity", "relation"):
            for row in self.rows[self.names[kind]]:
                row["chunk_ids"] = ids
        return second

    async def test_two_source_incremental_can_retain_old_display_path(self):
        second = self.append_incremental_source_with_old_paths()
        with patch("knowgrain.core_audit._audit_connection", AsyncMock(return_value=self.rows)):
            result = await audit_persisted_workspace_content(
                [self.manifest, second],
                self.config,
                self.identity,
                tokenizer=self.tokenizer,
                tokenizer_cache_dir=self.cache,
            )
        self.assertTrue(result.passed)

    async def test_synchronized_graph_and_vector_path_corruption_rejected(self):
        self.graph()
        original = copy.deepcopy(self.rows)
        for kind, graph_table in (
            ("entity", "lightrag_graph_nodes"),
            ("relation", "lightrag_graph_edges"),
        ):
            with self.subTest(kind=kind):
                self.rows = copy.deepcopy(original)
                self.rows[graph_table][0]["properties"]["file_path"] = "/outside/unrelated.pdf"
                self.rows[self.names[kind]][0]["file_path"] = "/outside/unrelated.pdf"
                result = await self.audit()
                self.assertFalse(result.passed)
                self.assertNotIn("/outside", repr(result))

    async def test_multiple_members_exact_sets_and_member_boundary(self):
        other_manifest = self.builder.build(b"second synthetic source", revision_id=UUID(int=2))
        other_fixture = AuditTests()
        other_fixture.manifest = other_manifest
        other_fixture.setUp()
        for name, values in other_fixture.rows.items():
            self.rows[name].extend(values)
        with patch("knowgrain.core_audit._audit_connection", AsyncMock(return_value=self.rows)):
            result = await audit_persisted_workspace_content(
                [self.manifest, other_manifest],
                self.config,
                self.identity,
                tokenizer=self.tokenizer,
                tokenizer_cache_dir=self.cache,
            )
            self.assertTrue(result.passed)
        self.assertFalse((await self.audit()).passed)
        self.assertTrue((await self.audit(False)).passed)

    async def test_capped_projection_and_truncated_graph_vector_text(self):
        from knowgrain.index_profile import IndexProfile

        profile = self.builder.profile_options(size=10, overlap=0, embedding=5)
        payload = json.loads(profile.to_canonical_json())
        payload["graph"]["knobs"].update(max_source_ids_per_entity=2, max_source_ids_per_relation=2)
        for method in ("KEEP", "FIFO"):
            payload["graph"]["knobs"]["source_ids_limit_method"] = method
            profile = IndexProfile.from_canonical_json(json.dumps(payload))
            self.manifest = self.builder.build(b"synthetic words " * 20, profile=profile)
            self.setUp()
            self.graph()
            chunks = [c.id for c in self.manifest.chunks]
            self.assertGreater(len(chunks), 2)
            projection = chunks[:2] if method == "KEEP" else chunks[-2:]
            for table in ("lightrag_graph_nodes", "lightrag_graph_edges"):
                for row in self.rows[table]:
                    row["properties"]["source_id"] = "<SEP>".join(projection)
            for kind in ("entity", "relation"):
                for row in self.rows[self.names[kind]]:
                    row["chunk_ids"] = projection
                    content = row["content"]
                    span = self.tokenizer.truncate_by_token_limit(content, 5)
                    row["content"] = content[span.start : span.end]
            self.assertTrue((await self.audit()).passed)
            self.rows[self.names["entity"]][0]["chunk_ids"] = chunks
            self.assertFalse((await self.audit()).passed)

    async def test_missing_tokenizer_and_mismatched_workspace_fail_before_connect(self):
        with patch("knowgrain.core_audit.asyncpg.connect", AsyncMock()) as connect:
            with self.assertRaises(AuditError):
                await audit_persisted_content(self.manifest, self.config, self.identity)
            config = ResolvedPostgresConfig("localhost", 55434, "test", "test", workspace="wrong")
            with self.assertRaises(AuditError):
                await audit_persisted_content(
                    self.manifest,
                    config,
                    self.identity,
                    tokenizer=self.tokenizer,
                    tokenizer_cache_dir=self.cache,
                )
            connect.assert_not_called()

    async def test_duplicate_manifest_and_profile_mismatch_rejected(self):
        from dataclasses import replace

        with self.assertRaises(AuditError):
            await audit_persisted_workspace_content(
                [self.manifest, self.manifest],
                self.config,
                self.identity,
                tokenizer=self.tokenizer,
                tokenizer_cache_dir=self.cache,
            )
        other = replace(self.manifest, revision_id=UUID(int=2), profile_snapshot_fingerprint="bad")
        with self.assertRaises(AuditError):
            await audit_persisted_workspace_content(
                [self.manifest, other],
                self.config,
                self.identity,
                tokenizer=self.tokenizer,
                tokenizer_cache_dir=self.cache,
            )


class AuditConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = ResolvedPostgresConfig("localhost", 55434, "test", "test", timeout=0.05)
        self.identity = CoreIndexIdentity("audit", Path("/tmp/audit"))

    def connection(self):
        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Connection:
            close = AsyncMock()
            terminate = unittest.mock.Mock()
            fetch = AsyncMock(return_value=[])

            def transaction(self, **kwargs):
                self.transaction_kwargs = kwargs
                return Transaction()

        return Connection()

    async def test_query_failure_is_sanitized_and_closed(self):
        from knowgrain.core_audit import _audit_connection

        conn = self.connection()
        conn.fetch.side_effect = RuntimeError("private document and password")
        with (
            patch("knowgrain.core_audit.asyncpg.connect", AsyncMock(return_value=conn)),
            patch("knowgrain.core_audit._inspect_schema", AsyncMock(return_value=[])),
            self.assertRaises(AuditError) as error,
        ):
            await _audit_connection(self.config, self.identity, 2)
        self.assertNotIn("private", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        conn.close.assert_awaited_once()
        self.assertEqual(
            conn.transaction_kwargs, {"isolation": "repeatable_read", "readonly": True}
        )

    async def test_schema_failure_cannot_be_empty_success(self):
        from knowgrain.core_audit import _audit_connection

        conn = self.connection()
        with (
            patch("knowgrain.core_audit.asyncpg.connect", AsyncMock(return_value=conn)),
            patch(
                "knowgrain.core_audit._inspect_schema",
                AsyncMock(side_effect=AuditError("Rejected")),
            ),
            self.assertRaises(AuditError),
        ):
            await _audit_connection(self.config, self.identity, 2)
        conn.close.assert_awaited_once()
        conn.fetch.assert_not_awaited()

    async def test_close_failure_terminates_and_rejects(self):
        from knowgrain.core_audit import _audit_connection

        conn = self.connection()
        conn.close = AsyncMock(side_effect=RuntimeError("private"))
        with (
            patch("knowgrain.core_audit.asyncpg.connect", AsyncMock(return_value=conn)),
            patch("knowgrain.core_audit._inspect_schema", AsyncMock(return_value=[])),
            self.assertRaisesRegex(AuditError, "cleanup"),
        ):
            await _audit_connection(self.config, self.identity, 2)
        conn.terminate.assert_called_once()

    async def test_timeout_and_cancellation_close(self):
        import asyncio

        from knowgrain.core_audit import _audit_connection

        async def hang(*args):
            await asyncio.Event().wait()

        for cancel in (False, True):
            conn = self.connection()
            conn.fetch = AsyncMock(side_effect=hang)
            with (
                patch("knowgrain.core_audit.asyncpg.connect", AsyncMock(return_value=conn)),
                patch("knowgrain.core_audit._inspect_schema", AsyncMock(return_value=[])),
            ):
                task = asyncio.create_task(_audit_connection(self.config, self.identity, 2))
                if cancel:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else AuditError):
                    await task
            conn.close.assert_awaited_once()


class FilePathMembershipTests(unittest.TestCase):
    def validate(self, path, method="KEEP", limit=2, candidates=3):
        from knowgrain.core_audit import _validate_file_path_membership

        issues = []
        chunks = {
            "c1": {"file_path": "source-a"},
            "c2": {"file_path": "source-b"},
            "c3": {"file_path": "source-c"},
            "c4": {"file_path": "source-a"},
        }
        if candidates == 4:
            chunks["c4"]["file_path"] = "source-d"
        _validate_file_path_membership(
            path,
            list(chunks),
            chunks,
            {
                "max_file_paths": limit,
                "source_ids_limit_method": method,
                "file_path_more_placeholder": "more",
            },
            issues,
        )
        return issues

    def test_uncapped_allows_historical_unique_member_subset(self):
        self.assertFalse(self.validate("source-c<SEP>source-b<SEP>source-a", limit=3))
        self.assertFalse(self.validate("source-a", limit=3))
        self.assertFalse(self.validate("source-a<SEP>...more...(KEEP Old)", limit=3))
        for path in (
            "source-a<SEP>source-b<SEP>source-c<SEP>source-a",
            "unrelated",
            "",
            " ",
            "...more...(KEEP Old)",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.validate(path, limit=3))

    def test_overflow_allows_only_known_marker_and_member_subset(self):
        for method, marker in (("KEEP", "KEEP Old"), ("FIFO", "FIFO")):
            for ending in (marker, f"{method} 2/3"):
                self.assertFalse(
                    self.validate(f"source-a<SEP>source-c<SEP>...more...({ending})", method)
                )
            self.assertFalse(self.validate("source-a<SEP>source-b", method))
            for path in (
                "...more...(" + marker + ")",
                "unrelated<SEP>...more...(" + marker + ")",
                "source-a<SEP>...wrong...(" + marker + ")",
                "source-a<SEP>source-b<SEP>source-c<SEP>...more...(" + marker + ")",
                "source-a<SEP>source-a<SEP>...more...(" + marker + ")",
                "source-a<SEP>...more...(" + marker + ")<SEP>...more...(" + marker + ")",
                f"source-a<SEP>...more...({method} 2/2)",
            ):
                with self.subTest(method=method, path=path):
                    self.assertTrue(self.validate(path, method))

    def test_zero_limit_accepts_only_known_marker_and_no_real_paths(self):
        self.assertFalse(self.validate("...more...(KEEP Old)", limit=0))
        self.assertTrue(self.validate("source-a<SEP>...more...(KEEP Old)", limit=0))
        self.assertTrue(self.validate("", limit=0))

    def test_historical_denominator_need_not_equal_current_candidates(self):
        for method in ("KEEP", "FIFO"):
            for denominator in (3, 4, 99):
                self.assertFalse(
                    self.validate(
                        f"source-a<SEP>...more...({method} 2/{denominator})", method, candidates=4
                    )
                )
            other_method = "FIFO" if method == "KEEP" else "KEEP"
            for marker in (
                f"{method} 1/3",
                f"{method} 3/4",
                f"{method} 2/2",
                f"{method} 2/0",
                f"{method} 2/-3",
                f"{method} 2/03",
                f"{method} 2/+3",
                f"{method} 2/3.0",
                f"{other_method} 2/3",
            ):
                with self.subTest(method=method, marker=marker):
                    self.assertTrue(
                        self.validate(f"source-a<SEP>...more...({marker})", method, candidates=4)
                    )
