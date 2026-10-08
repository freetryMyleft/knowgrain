"""Committed synthetic schemas ONLY in the explicitly owned audit PostgreSQL database.

Never runs on the usual Knowgrain app/Core databases. Setup refuses any existing
LightRAG objects; teardown drops only the objects this fixture itself created.
"""

import os
import unittest
from pathlib import Path
from uuid import uuid4

import asyncpg

from knowgrain.core_preflight import (
    PreflightError,
    ResolvedPostgresConfig,
    _pinned_ddls,
    _q,
    inspect_fresh_rebuild_target,
    target_vector_table_names,
)
from knowgrain.index_identity import CoreIndexIdentity


@unittest.skipUnless(
    os.environ.get("KNOWGRAIN_AUDIT_TEST_DATABASE") == "knowgrain_audit_test"
    and os.environ.get("KNOWGRAIN_AUDIT_TEST_PORT") == "55434",
    "explicitly owned knowgrain_audit_test on port 55434 not selected",
)
class FreshTargetPostgresTests(unittest.IsolatedAsyncioTestCase):
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
        self.identity = CoreIndexIdentity(
            "preflight-" + uuid4().hex, Path("/tmp/preflight"), "kg_" + uuid4().hex[:24]
        )
        existing = await self.conn.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_class
            WHERE lower(relname) LIKE '%lightrag%')""")
        if existing:
            await self.conn.close()
            self.fail("Audit fixture refuses existing LightRAG objects")
        ddls = _pinned_ddls()
        try:
            for name, ddl in ddls.items():
                if "_vdb_" not in name:
                    await self.conn.execute(ddl)
                    self.tables.append(name)
            from lightrag.kg.pgtable_impl import _DDL

            await self.conn.execute(_DDL)
            self.tables.extend(["lightrag_graph_nodes", "lightrag_graph_edges"])
        except BaseException:
            await self.asyncTearDown()
            raise

    async def asyncTearDown(self):
        for schema in reversed(self.schemas):
            await self.conn.execute(f"DROP SCHEMA {_q(schema)} CASCADE")
        for name in reversed(self.tables):
            await self.conn.execute(f"DROP TABLE IF EXISTS public.{_q(name)} CASCADE")
        await self.conn.close()

    async def inspect(self):
        return await inspect_fresh_rebuild_target(self.config, self.identity, 3)

    async def rejected(self, sql, reason=None):
        # Commit corruption for the inspector's separate connection, then restore
        # it via an explicit fixture snapshot SQL supplied by each test.
        await self.conn.execute(sql)
        with self.assertRaises(PreflightError) as error:
            await self.inspect()
        if reason:
            self.assertIn(reason, str(error.exception))
        return error.exception

    async def vector_table(self, base, suffixed=False, workspace="other"):
        name = base + ("_other_3d" if suffixed else "")
        ddl = (
            _pinned_ddls()[base]
            .replace(base.upper(), name.upper())
            .replace("VECTOR(dimension)", "VECTOR(3)")
        )
        await self.conn.execute(ddl)
        self.tables.append(name)
        if workspace is not None:
            await self.conn.execute(
                f"INSERT INTO {_q(name)}(workspace,id) VALUES($1,'old')", workspace
            )
        return name

    async def test_healthy_snapshot_preserves_other_workspace_and_catalog(self):
        for name in self.tables:
            if "graph_" not in name:
                await self.conn.execute(
                    f"INSERT INTO {_q(name)}(workspace,id) VALUES('other','extract:old')"
                )
        await self.conn.execute(
            "INSERT INTO lightrag_graph_nodes(workspace,namespace,id) VALUES('other','custom','z'),('other','custom','a')"
        )
        await self.conn.execute(
            "INSERT INTO lightrag_graph_edges(workspace,namespace,src_id,tgt_id) VALUES('other','custom','z','a')"
        )
        for base in ("lightrag_vdb_entity", "lightrag_vdb_relation", "lightrag_vdb_chunks"):
            await self.vector_table(base)
            await self.vector_table(base, True)
        before = await self.conn.fetch(
            "SELECT oid,relname,relfilenode FROM pg_class WHERE relname LIKE '%lightrag%' ORDER BY oid"
        )

        async def rows():
            return {
                name: await self.conn.fetch(
                    f"SELECT to_jsonb(t)::text AS data FROM public.{_q(name)} t ORDER BY to_jsonb(t)::text"
                )
                for name in self.tables
            }

        async def definitions():
            return await self.conn.fetch("""SELECT c.oid,pg_catalog.to_jsonb(c)::text AS definition
                FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class r ON r.oid=c.conrelid
                WHERE r.relname LIKE '%lightrag%' ORDER BY c.oid""")

        data_before, definitions_before = await rows(), await definitions()
        report = await self.inspect()
        self.assertEqual(data_before, await rows())
        self.assertEqual(definitions_before, await definitions())
        self.assertEqual(report.workspace, self.identity.workspace)
        self.assertEqual(len(report.inspected_tables), 16)
        self.assertEqual(
            before,
            await self.conn.fetch(
                "SELECT oid,relname,relfilenode FROM pg_class WHERE relname LIKE '%lightrag%' ORDER BY oid"
            ),
        )
        self.assertEqual(await self.conn.fetchval("SELECT src_id FROM lightrag_graph_edges"), "z")

    async def test_every_nonvector_workspace_and_graph_namespace_is_checked(self):
        for name in self.tables:
            if name.endswith("edges"):
                continue
            namespace = ",namespace" if "graph_" in name else ""
            value = ",'unexpected'" if namespace else ""
            await self.conn.execute(
                f"INSERT INTO {_q(name)}(workspace,id{namespace}) VALUES($1,'polluted'{value})",
                self.identity.workspace,
            )
            with self.assertRaisesRegex(PreflightError, "already contains"):
                await self.inspect()
            await self.conn.execute(
                f"DELETE FROM {_q(name)} WHERE workspace=$1", self.identity.workspace
            )

    async def test_each_vector_family_and_model_workspace_checked(self):
        for base in ("lightrag_vdb_entity", "lightrag_vdb_relation", "lightrag_vdb_chunks"):
            for suffixed in (False, True):
                name = await self.vector_table(base, suffixed, self.identity.workspace)
                with self.assertRaisesRegex(PreflightError, "already contains"):
                    await self.inspect()
                await self.conn.execute(f"UPDATE {_q(name)} SET workspace='other'")

    async def test_empty_base_and_existing_empty_target_rejected(self):
        base = await self.vector_table("lightrag_vdb_entity", workspace=None)
        with self.assertRaisesRegex(PreflightError, "would be dropped"):
            await self.inspect()
        await self.conn.execute(f"DROP TABLE {_q(base)}")
        target = target_vector_table_names(self.identity, 3)[0]
        await self.conn.execute(f"CREATE TABLE {_q(target)}(id text)")
        self.tables.append(target)
        with self.assertRaisesRegex(PreflightError, "already exists"):
            await self.inspect()

    async def test_named_fk_rename_missing_not_valid_and_wrong_keys(self):
        await self.rejected(
            "ALTER TABLE lightrag_graph_edges RENAME CONSTRAINT fk_lightrag_graph_edges_src TO renamed_fk",
            "foreign key",
        )
        await self.conn.execute(
            "ALTER TABLE lightrag_graph_edges RENAME CONSTRAINT renamed_fk TO fk_lightrag_graph_edges_src"
        )
        await self.rejected(
            "ALTER TABLE lightrag_graph_edges DROP CONSTRAINT fk_lightrag_graph_edges_src",
            "foreign key",
        )
        for target, delete, validated in [
            ("src_id", "CASCADE", "NOT VALID"),
            ("tgt_id", "CASCADE", ""),
            ("src_id", "RESTRICT", ""),
        ]:
            await self.rejected(
                f"ALTER TABLE lightrag_graph_edges ADD CONSTRAINT fk_lightrag_graph_edges_src FOREIGN KEY(workspace,namespace,{target}) REFERENCES lightrag_graph_nodes(workspace,namespace,id) ON DELETE {delete} {validated}",
                "foreign key",
            )
            await self.conn.execute(
                "ALTER TABLE lightrag_graph_edges DROP CONSTRAINT fk_lightrag_graph_edges_src"
            )

    async def test_pk_names_and_order_rejected(self):
        await self.rejected(
            "ALTER TABLE lightrag_graph_nodes RENAME CONSTRAINT lightrag_graph_nodes_pkey TO renamed_pk",
            "primary key",
        )
        await self.conn.execute(
            "ALTER TABLE lightrag_graph_nodes RENAME CONSTRAINT renamed_pk TO lightrag_graph_nodes_pkey"
        )
        await self.rejected(
            "ALTER TABLE lightrag_full_entities DROP CONSTRAINT lightrag_full_entities_pk; ALTER TABLE lightrag_full_entities ADD PRIMARY KEY(id,workspace)",
            "primary key",
        )

    async def test_old_columns_timestamps_cache_and_index_rejected(self):
        for sql, undo in [
            (
                "ALTER TABLE lightrag_llm_cache ADD COLUMN mode text",
                "ALTER TABLE lightrag_llm_cache DROP COLUMN mode",
            ),
            (
                "ALTER TABLE lightrag_doc_chunks ADD COLUMN content_vector vector(3)",
                "ALTER TABLE lightrag_doc_chunks DROP COLUMN content_vector",
            ),
            (
                "ALTER TABLE lightrag_doc_chunks RENAME COLUMN heading TO old_heading",
                "ALTER TABLE lightrag_doc_chunks RENAME COLUMN old_heading TO heading",
            ),
            (
                "ALTER TABLE lightrag_doc_full ALTER COLUMN create_time TYPE timestamptz",
                "ALTER TABLE lightrag_doc_full ALTER COLUMN create_time TYPE timestamp(0)",
            ),
            (
                "INSERT INTO lightrag_llm_cache(workspace,id) VALUES('other','legacy')",
                "DELETE FROM lightrag_llm_cache",
            ),
            (
                "CREATE INDEX idx_lightrag_doc_status_ws_status_created_id ON lightrag_doc_status(workspace,status,created_at,id)",
                "DROP INDEX idx_lightrag_doc_status_ws_status_created_id",
            ),
        ]:
            await self.rejected(sql)
            await self.conn.execute(undo)

    async def test_cross_schema_ambiguity_and_missing_table_rejected(self):
        schema = "audit_" + uuid4().hex
        await self.conn.execute(f"CREATE SCHEMA {_q(schema)}")
        self.schemas.append(schema)
        await self.rejected(
            f"CREATE TABLE {_q(schema)}.lightrag_doc_full(id text)", "outside public"
        )
        await self.conn.execute(f"DROP TABLE {_q(schema)}.lightrag_doc_full")
        await self.rejected("DROP TABLE lightrag_full_entities", "missing")

    async def test_user_schema_resolution_and_disabled_fk_rejected(self):
        # Default "$user", public search_path now resolves an actual user schema.
        name = self.config.user
        exists = await self.conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)", name
        )
        if exists:
            self.fail("Audit fixture refuses an existing user schema")
        await self.conn.execute(f"CREATE SCHEMA {_q(name)}")
        self.schemas.append(name)
        with self.assertRaisesRegex(PreflightError, "schema resolution"):
            await self.inspect()
        await self.conn.execute(f"DROP SCHEMA {_q(name)}")
        self.schemas.remove(name)
        await self.rejected("ALTER TABLE lightrag_graph_edges DISABLE TRIGGER ALL", "enforcement")
        await self.conn.execute("ALTER TABLE lightrag_graph_edges ENABLE TRIGGER ALL")
        await self.inspect()

    async def test_rls_and_vector_column_type_rejected(self):
        await self.rejected(
            "ALTER TABLE lightrag_doc_status ALTER COLUMN metadata SET NOT NULL", "nullability"
        )
        await self.conn.execute(
            "ALTER TABLE lightrag_doc_status ALTER COLUMN metadata DROP NOT NULL"
        )
        await self.rejected(
            "ALTER TABLE lightrag_doc_full ENABLE ROW LEVEL SECURITY", "row security"
        )
        await self.conn.execute("ALTER TABLE lightrag_doc_full DISABLE ROW LEVEL SECURITY")
        name = await self.vector_table("lightrag_vdb_chunks", suffixed=True)
        await self.rejected(
            f"ALTER TABLE {_q(name)} ALTER COLUMN content_vector TYPE real[] USING NULL",
            "column type",
        )

    async def test_readonly_lock_timeout_and_actual_cancel_close(self):
        import asyncio
        from dataclasses import replace
        from unittest.mock import patch

        await self.conn.execute("BEGIN; LOCK lightrag_doc_full IN ACCESS EXCLUSIVE MODE")
        try:
            with self.assertRaisesRegex(PreflightError, "inspection failed"):
                await inspect_fresh_rebuild_target(
                    replace(self.config, timeout=0.1), self.identity, 3
                )
            original_connect = asyncpg.connect
            captured = []
            connected = asyncio.Event()

            async def connect(**kwargs):
                conn = await original_connect(**kwargs)
                captured.append(conn)
                connected.set()
                return conn

            with patch("knowgrain.core_preflight.asyncpg.connect", connect):
                task = asyncio.create_task(self.inspect())
                await connected.wait()
                await asyncio.sleep(0.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(captured[0].is_closed())
        finally:
            await self.conn.execute("ROLLBACK")
        await self.inspect()

    async def test_named_fk_wrong_constraint_type_and_reference_oid(self):
        await self.rejected(
            "ALTER TABLE lightrag_graph_edges DROP CONSTRAINT fk_lightrag_graph_edges_src; "
            "ALTER TABLE lightrag_graph_edges ADD CONSTRAINT fk_lightrag_graph_edges_src CHECK (src_id IS NOT NULL)",
            "foreign key",
        )
        await self.conn.execute(
            "ALTER TABLE lightrag_graph_edges DROP CONSTRAINT fk_lightrag_graph_edges_src"
        )
        clone = "audit_graph_nodes_" + uuid4().hex[:16]
        await self.conn.execute(
            f"CREATE TABLE {_q(clone)} (LIKE lightrag_graph_nodes INCLUDING ALL)"
        )
        self.tables.append(clone)
        await self.rejected(
            f"ALTER TABLE lightrag_graph_edges ADD CONSTRAINT fk_lightrag_graph_edges_src "
            f"FOREIGN KEY(workspace,namespace,src_id) REFERENCES {_q(clone)}(workspace,namespace,id) ON DELETE CASCADE",
            "foreign key",
        )
