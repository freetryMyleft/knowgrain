"""Committed corruption fixtures in the explicitly selected isolated audit database.

No user database is selected. Setup refuses preexisting LightRAG objects and
teardown removes only fixture-owned objects; originals and other workspaces are
never modified by the auditor.
"""

import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

import asyncpg
import test_core_audit as unit

from knowgrain.core_audit import (
    AuditError,
    _q,
    _vector_table_names,
    audit_persisted_workspace_content,
)
from knowgrain.core_preflight import ResolvedPostgresConfig, _pinned_ddls
from knowgrain.index_identity import CoreIndexIdentity


@unittest.skipUnless(
    os.environ.get("KNOWGRAIN_AUDIT_TEST_DATABASE") == "knowgrain_audit_test"
    and os.environ.get("KNOWGRAIN_AUDIT_TEST_PORT") == "55434",
    "explicitly owned knowgrain_audit_test on port 55434 not selected",
)
class PersistedAuditTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        unit.AuditTests.setUpClass()
        cls.addClassCleanup(unit.AuditTests.doClassCleanups)
        cls.manifest = unit.AuditTests.manifest
        cls.tokenizer = unit.AuditTests.tokenizer
        cls.cache = unit.AuditTests.cache

    async def asyncSetUp(self):
        self.config = ResolvedPostgresConfig(
            "127.0.0.1",
            55434,
            os.environ.get("KNOWGRAIN_AUDIT_TEST_USER", "knowgrain"),
            "knowgrain_audit_test",
            os.environ.get("KNOWGRAIN_AUDIT_TEST_PASSWORD", "knowgrain-local"),
            "disable",
        )
        self.conn = await asyncpg.connect(**self.config.connect_kwargs())
        self.tables = []
        self.schemas = []
        existing = await self.conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_class WHERE lower(relname) LIKE '%lightrag%')"
        )
        if existing:
            await self.conn.close()
            self.fail("Audit fixture refuses preexisting LightRAG objects")
        self.identity = CoreIndexIdentity(
            "e3-" + uuid4().hex, Path("/tmp/e3"), "kg_" + uuid4().hex[:24]
        )
        self.names = _vector_table_names(self.identity, 2)
        self.example = unit.AuditTests()
        self.example.setUp()
        old_names = self.example.names
        self.example.names = self.names
        self.rows = {
            self.names.get(next((k for k, v in old_names.items() if v == name), ""), name): value
            for name, value in self.example.rows.items()
        }
        self.example.rows = self.rows
        try:
            for name, ddl in _pinned_ddls().items():
                if "_vdb_" not in name:
                    await self.conn.execute(ddl)
                    self.tables.append(name)
            from lightrag.kg.pgtable_impl import _DDL

            await self.conn.execute(_DDL)
            self.tables.extend(["lightrag_graph_nodes", "lightrag_graph_edges"])
            for kind, name in self.names.items():
                base = "lightrag_vdb_" + kind
                await self.conn.execute(
                    _pinned_ddls()[base]
                    .replace(base.upper(), name.upper())
                    .replace("VECTOR(dimension)", "VECTOR(2)")
                )
                self.tables.append(name)
            self.example.graph()
            await self.persist()
        except BaseException:
            await self.asyncTearDown()
            raise

    async def asyncTearDown(self):
        for schema in reversed(self.schemas):
            await self.conn.execute(f"DROP SCHEMA {_q(schema)} CASCADE")
        for table in reversed(self.tables):
            await self.conn.execute(f"DROP TABLE IF EXISTS public.{_q(table)} CASCADE")
        await self.conn.close()

    async def persist(self):
        json_columns = {
            "metadata",
            "meta",
            "chunk_options",
            "chunks_list",
            "llm_cache_list",
            "heading",
            "sidecar",
            "entity_names",
            "relation_pairs",
            "properties",
        }
        for name, records in self.rows.items():
            for record in records:
                row = dict(record, workspace=self.identity.workspace)
                if "vector_text" in row:
                    row["content_vector"] = row.pop("vector_text")
                # Synthetic in-memory chunk vector rows contain text-only metadata.
                if name == self.names["chunks"]:
                    for key in ("heading", "sidecar", "llm_cache_list"):
                        row.pop(key, None)
                columns = list(row)
                values = [
                    json.dumps(row[k])
                    if row[k] is not None
                    and (k in json_columns or (k == "chunk_ids" and "_vdb_" not in name))
                    else row[k]
                    for k in columns
                ]
                placeholders = [
                    f"${i + 1}::text::vector" if k == "content_vector" else f"${i + 1}"
                    for i, k in enumerate(columns)
                ]
                await self.conn.execute(
                    f"INSERT INTO public.{_q(name)}({','.join(_q(k) for k in columns)}) VALUES({','.join(placeholders)})",
                    *values,
                )

    async def audit(self):
        return await audit_persisted_workspace_content(
            [self.manifest],
            self.config,
            self.identity,
            tokenizer=self.tokenizer,
            tokenizer_cache_dir=self.cache,
        )

    async def corrupt(self, sql):
        await self.conn.execute(sql)
        result = await self.audit()
        self.assertFalse(result.passed)
        self.assertNotIn("synthetic description", repr(result))
        self.assertNotIn("synthetic audit text", repr(result))

    async def test_healthy_committed_snapshot_and_other_workspace_preserved(self):
        await self.conn.execute(
            "INSERT INTO lightrag_doc_full(workspace,id,content) VALUES('other','other','synthetic other')"
        )
        before = {
            t: await self.conn.fetch(
                f"SELECT to_jsonb(t)::text AS data FROM public.{_q(t)} t ORDER BY to_jsonb(t)::text"
            )
            for t in self.tables
        }
        self.assertTrue((await self.audit()).passed)
        after = {
            t: await self.conn.fetch(
                f"SELECT to_jsonb(t)::text AS data FROM public.{_q(t)} t ORDER BY to_jsonb(t)::text"
            )
            for t in self.tables
        }
        self.assertEqual(before, after)

    async def test_document_chunk_and_vector_corruptions(self):
        for sql, undo in [
            (
                "UPDATE lightrag_doc_full SET content_hash='bad'",
                "UPDATE lightrag_doc_full SET content_hash=$1",
            ),
            (
                "UPDATE lightrag_doc_status SET chunks_count=99",
                "UPDATE lightrag_doc_status SET chunks_count=1",
            ),
            (
                "UPDATE lightrag_doc_chunks SET tokens=999",
                "UPDATE lightrag_doc_chunks SET tokens=$1",
            ),
            (
                f"UPDATE {_q(self.names['chunks'])} SET content_vector=NULL",
                f"UPDATE {_q(self.names['chunks'])} SET content_vector='[1,2]'",
            ),
        ]:
            await self.corrupt(sql)
            if "$1" in undo:
                value = (
                    self.manifest.core_sha256 if "hash" in undo else self.manifest.chunks[0].tokens
                )
                await self.conn.execute(undo, value)
            else:
                await self.conn.execute(undo)
            self.assertTrue((await self.audit()).passed)

    async def incremental_sources_fixture(self):
        # Replace only fixture-owned rows with the two-source durable state.
        for table in reversed(self.tables):
            await self.conn.execute(f"DELETE FROM public.{_q(table)}")
        self.example.setUp()
        old_names = self.example.names
        second = self.example.append_incremental_source_with_old_paths()
        self.rows = {
            self.names.get(next((k for k, v in old_names.items() if v == name), ""), name): value
            for name, value in self.example.rows.items()
        }
        await self.persist()
        return second

    async def test_two_source_incremental_can_retain_old_graph_and_vector_paths(self):
        second = await self.incremental_sources_fixture()
        result = await audit_persisted_workspace_content(
            [self.manifest, second],
            self.config,
            self.identity,
            tokenizer=self.tokenizer,
            tokenizer_cache_dir=self.cache,
        )
        self.assertTrue(result.passed)

    async def test_two_source_synchronized_single_entity_path_corruption(self):
        second = await self.incremental_sources_fixture()

        async def observe():
            return await audit_persisted_workspace_content(
                [self.manifest, second],
                self.config,
                self.identity,
                tokenizer=self.tokenizer,
                tokenizer_cache_dir=self.cache,
            )

        self.assertTrue((await observe()).passed)
        await self.conn.execute(
            "UPDATE lightrag_graph_nodes SET properties=jsonb_set(properties,'{file_path}',to_jsonb($1::text)) WHERE workspace=$2 AND id='A'",
            "/outside/unrelated.pdf",
            self.identity.workspace,
        )
        await self.conn.execute(
            f"UPDATE {_q(self.names['entity'])} SET file_path=$1 WHERE workspace=$2 AND entity_name='A'",
            "/outside/unrelated.pdf",
            self.identity.workspace,
        )
        result = await observe()
        self.assertFalse(result.passed)
        self.assertEqual(
            [issue.message for issue in result.issues],
            ["Graph source paths or truncation marker disagree with tracking sources"],
        )
        self.assertNotIn("/outside", repr(result))

    async def test_synchronized_graph_and_vector_paths_cannot_leave_tracking_sources(self):
        for kind, table, predicate, vector_predicate in (
            ("entity", "lightrag_graph_nodes", "id='A'", "entity_name='A'"),
            (
                "relation",
                "lightrag_graph_edges",
                "src_id='A' AND tgt_id='B'",
                "source_id='A' AND target_id='B'",
            ),
        ):
            await self.conn.execute(
                f"UPDATE {_q(table)} SET properties=jsonb_set(properties,'{{file_path}}',to_jsonb($1::text)) WHERE {predicate}",
                "/outside/unrelated.pdf",
            )
            await self.conn.execute(
                f"UPDATE {_q(self.names[kind])} SET file_path=$1 WHERE {vector_predicate}",
                "/outside/unrelated.pdf",
            )
            result = await self.audit()
            self.assertFalse(result.passed)
            self.assertNotIn("/outside", repr(result))
            await self.conn.execute(
                f"UPDATE {_q(table)} SET properties=jsonb_set(properties,'{{file_path}}',to_jsonb($1::text)) WHERE {predicate}",
                self.manifest.canonical_file_path,
            )
            await self.conn.execute(
                f"UPDATE {_q(self.names[kind])} SET file_path=$1 WHERE {vector_predicate}",
                self.manifest.canonical_file_path,
            )
            self.assertTrue((await self.audit()).passed)

    async def test_missing_document_and_orphan_records(self):
        await self.corrupt("DELETE FROM lightrag_doc_full")

    async def test_graph_tracking_provenance_and_indexed_text_corruptions(self):
        for sql, undo in [
            (
                "UPDATE lightrag_entity_chunks SET count=99",
                "UPDATE lightrag_entity_chunks SET count=1",
            ),
            (
                "UPDATE lightrag_relation_chunks SET id='A->B'",
                "UPDATE lightrag_relation_chunks SET id='A<SEP>B'",
            ),
            (
                "UPDATE lightrag_graph_nodes SET properties=jsonb_set(properties,'{source_id}','\"missing\"') WHERE id='A'",
                "UPDATE lightrag_graph_nodes SET properties=jsonb_set(properties,'{source_id}',to_jsonb($1::text)) WHERE id='A'",
            ),
            (
                f"UPDATE {_q(self.names['entity'])} SET content='wrong' WHERE entity_name='A'",
                f"UPDATE {_q(self.names['entity'])} SET content='A'||chr(10)||'synthetic description' WHERE entity_name='A'",
            ),
        ]:
            await self.corrupt(sql)
            if "$1" in undo:
                await self.conn.execute(undo, self.manifest.chunks[0].id)
            else:
                await self.conn.execute(undo)
            self.assertTrue((await self.audit()).passed)

    async def test_unexpected_graph_namespace_and_extra_workspace_document(self):
        await self.corrupt(
            "INSERT INTO lightrag_graph_nodes(workspace,namespace,id) SELECT workspace,'unexpected','X' FROM lightrag_doc_full LIMIT 1"
        )
        await self.conn.execute("DELETE FROM lightrag_graph_nodes WHERE namespace='unexpected'")
        await self.corrupt(
            "INSERT INTO lightrag_doc_full(workspace,id) SELECT workspace,'unexpected' FROM lightrag_doc_full LIMIT 1"
        )

    async def test_missing_table_and_vector_dimension_fail_closed(self):
        name = self.names["chunks"]
        await self.conn.execute(
            f"ALTER TABLE {_q(name)} ALTER COLUMN content_vector TYPE vector(3) USING '[1,2,3]'::vector"
        )
        with self.assertRaises(AuditError):
            await self.audit()
        await self.conn.execute(
            f"ALTER TABLE {_q(name)} ALTER COLUMN content_vector TYPE vector(2) USING '[1,2]'::vector"
        )
        await self.conn.execute("DROP TABLE lightrag_relation_chunks")
        with self.assertRaises(AuditError):
            await self.audit()

    async def test_rls_and_shadow_schema_fail_closed(self):
        await self.conn.execute("ALTER TABLE lightrag_doc_full ENABLE ROW LEVEL SECURITY")
        with self.assertRaises(AuditError):
            await self.audit()
        await self.conn.execute("ALTER TABLE lightrag_doc_full DISABLE ROW LEVEL SECURITY")
        await self.conn.execute("CREATE SCHEMA audit_shadow")
        self.schemas.append("audit_shadow")
        await self.conn.execute("CREATE TABLE audit_shadow.lightrag_doc_full(id text)")
        with self.assertRaises(AuditError):
            await self.audit()

    async def test_other_vector_family_target_rows_rejected(self):
        name = "lightrag_vdb_entity_other_2d"
        await self.conn.execute(
            _pinned_ddls()["lightrag_vdb_entity"]
            .replace("LIGHTRAG_VDB_ENTITY", name.upper())
            .replace("VECTOR(dimension)", "VECTOR(2)")
        )
        self.tables.append(name)
        await self.conn.execute(
            f"INSERT INTO {_q(name)}(workspace,id) VALUES($1,'unexpected')", self.identity.workspace
        )
        with self.assertRaisesRegex(AuditError, "namespace"):
            await self.audit()
