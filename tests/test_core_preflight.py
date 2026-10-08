"""Configuration and connection lifecycle tests for the inert fresh-target inspector."""

import asyncio
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, patch

from knowgrain.core_preflight import (
    PreflightError,
    ResolvedPostgresConfig,
    _column_spec,
    _pinned_ddls,
    inspect_fresh_rebuild_target,
    target_vector_table_names,
)
from knowgrain.index_identity import CoreIndexIdentity


def identity():
    return CoreIndexIdentity("fresh-test", Path("/tmp/fresh-test"), "kg_" + "a" * 24)


def config():
    return ResolvedPostgresConfig.from_upstream(
        {
            "host": "127.0.0.1",
            "port": "55434",
            "user": "test",
            "database": "test",
            "password": "private-secret",
            "enable_vector": True,
            "vector_index_type": "HNSW",
        }
    )


class ConfigTests(unittest.TestCase):
    def test_resolved_config_is_frozen_and_secret_safe(self):
        value = config()
        self.assertNotIn("private-secret", repr(value))
        self.assertEqual(value.port, 55434)
        with self.assertRaises(FrozenInstanceError):
            value.port = 123
        self.assertNotIn("ssl", value.connect_kwargs())
        self.assertEqual(
            ResolvedPostgresConfig.from_upstream(
                value.connect_kwargs()
                | {"enable_vector": True, "vector_index_type": "HNSW", "server_settings": {}}
            ).database,
            "test",
        )

    def test_reject_unsupported_config(self):
        base = {
            "host": "localhost",
            "port": 5432,
            "user": "test",
            "database": "test",
            "enable_vector": True,
            "vector_index_type": "HNSW",
        }
        for key, value in [
            ("ssl_mode", "require"),
            ("ssl_cert", "private"),
            ("server_settings", "search_path=evil"),
            ("port", 0),
            ("host", ""),
            ("user", " "),
            ("database", None),
            ("port", True),
            ("port", 123.5),
            ("statement_cache_size", 2.2),
            ("enable_vector", False),
            ("vector_index_type", "VCHORDRQ"),
            ("statement_cache_size", "bad"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(PreflightError):
                ResolvedPostgresConfig.from_upstream(base | {key: value})
        for timeout in (0, 61, float("nan"), True, 10**1000):
            with self.assertRaises(PreflightError):
                ResolvedPostgresConfig("localhost", 5432, "test", "test", timeout=timeout)

    def test_no_environment_reresolution(self):
        with patch.dict("os.environ", {"POSTGRES_DATABASE": "wrong"}):
            self.assertEqual(config().database, "test")

    def test_target_names_and_guarded_schema(self):
        names = target_vector_table_names(identity(), 768)
        self.assertEqual(names[0], "lightrag_vdb_entity_kg_" + "a" * 24 + "_768d")
        self.assertEqual(len(names), 3)
        self.assertEqual(
            _column_spec(_pinned_ddls()["lightrag_doc_full"])["parse_engine"], ("text", False)
        )
        for dimension in (True, 0, -1, 10**20):
            with self.assertRaises(PreflightError):
                target_vector_table_names(identity(), dimension)
        with self.assertRaises(PreflightError):
            target_vector_table_names(CoreIndexIdentity("old", Path("/tmp/old")), 768)

    def test_changed_source_rejected(self):
        with (
            patch("knowgrain.core_preflight._SOURCE_HASHES", {"postgres_impl.py": "bad"}),
            self.assertRaises(PreflightError),
        ):
            _pinned_ddls()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Connection:
            close = AsyncMock()

            def transaction(self, **kwargs):
                self.options = kwargs
                return Transaction()

            def terminate(self):
                self.terminated = True

        return Connection()

    async def test_sql_error_static_and_connection_closed(self):
        conn = self.connection()
        with (
            patch("knowgrain.core_preflight.asyncpg.connect", AsyncMock(return_value=conn)),
            patch(
                "knowgrain.core_preflight._inspect",
                AsyncMock(side_effect=RuntimeError("secret row")),
            ),
            self.assertRaises(PreflightError) as error,
        ):
            await inspect_fresh_rebuild_target(config(), identity(), 768)
        self.assertNotIn("secret", str(error.exception))
        conn.close.assert_awaited_once()
        self.assertEqual(conn.options, {"isolation": "repeatable_read", "readonly": True})

    async def test_connect_timeout_static(self):
        with (
            patch(
                "knowgrain.core_preflight.asyncpg.connect",
                AsyncMock(side_effect=TimeoutError("secret")),
            ),
            self.assertRaisesRegex(PreflightError, "inspection failed"),
        ):
            await inspect_fresh_rebuild_target(config(), identity(), 768)

    async def test_cancel_propagates_and_actual_close_finishes(self):
        conn = self.connection()
        started, closed = asyncio.Event(), asyncio.Event()

        async def inspect(*args):
            started.set()
            await asyncio.Event().wait()

        async def close(**kwargs):
            await asyncio.sleep(0.01)
            closed.set()

        conn.close = AsyncMock(side_effect=close)
        with (
            patch("knowgrain.core_preflight.asyncpg.connect", AsyncMock(return_value=conn)),
            patch("knowgrain.core_preflight._inspect", inspect),
        ):
            task = asyncio.create_task(inspect_fresh_rebuild_target(config(), identity(), 768))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())

    async def test_cancel_stays_cancelled_if_close_fails(self):
        conn = self.connection()
        conn.close = AsyncMock(side_effect=RuntimeError("private close diagnostic"))
        with (
            patch("knowgrain.core_preflight.asyncpg.connect", AsyncMock(return_value=conn)),
            patch(
                "knowgrain.core_preflight._inspect",
                AsyncMock(side_effect=asyncio.CancelledError("original")),
            ),
            self.assertRaises(asyncio.CancelledError) as error,
        ):
            await inspect_fresh_rebuild_target(config(), identity(), 3)
        self.assertEqual(str(error.exception), "original")
        self.assertTrue(conn.terminated)


class CatalogGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_search_path_encoding_replication_and_missing_schema(self):
        from knowgrain.core_preflight import _inspect

        healthy = {
            "schema_oid": 2200,
            "explicit_schemas": ["public"],
            "all_schemas": ["pg_catalog", "public"],
            "encoding": "UTF8",
            "replication_role": "origin",
        }
        for changes in (
            {"schema_oid": None},
            {"encoding": "LATIN1"},
            {"replication_role": "replica"},
            {"explicit_schemas": ["user", "public"]},
            {"all_schemas": ["pg_catalog", "pg_temp_3", "public"]},
        ):
            conn = AsyncMock()
            conn.fetchrow.return_value = healthy | changes
            with self.assertRaisesRegex(PreflightError, "schema resolution"):
                await _inspect(conn, identity(), (), {})
            conn.fetch.assert_not_awaited()

    async def test_missing_or_spoofed_extension_rejected(self):
        from knowgrain.core_preflight import _inspect

        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "schema_oid": 2200,
            "explicit_schemas": ["public"],
            "all_schemas": ["pg_catalog", "public"],
            "encoding": "UTF8",
            "replication_role": "origin",
        }
        for types in (
            [],
            [{"member": True, "nspname": "public"}],
            [{"member": True, "nspname": "public"}, {"member": False, "nspname": "public"}],
            [{"member": True, "nspname": "public"}, {"member": True, "nspname": "wrong"}],
        ):
            conn.fetch.return_value = types
            with self.assertRaisesRegex(PreflightError, "extension types"):
                await _inspect(conn, identity(), (), {})

    async def test_effective_workspace_mismatch_rejected_before_connect(self):
        from dataclasses import replace

        conn = AsyncMock()
        with (
            patch("knowgrain.core_preflight.asyncpg.connect", conn),
            self.assertRaisesRegex(PreflightError, "workspace differs"),
        ):
            await inspect_fresh_rebuild_target(replace(config(), workspace="wrong"), identity(), 3)
        conn.assert_not_awaited()

    async def test_permissions_failure_is_static(self):
        import asyncpg

        conn = LifecycleTests().connection()
        with (
            patch("knowgrain.core_preflight.asyncpg.connect", AsyncMock(return_value=conn)),
            patch(
                "knowgrain.core_preflight._inspect",
                AsyncMock(side_effect=asyncpg.InsufficientPrivilegeError("private table")),
            ),
            self.assertRaisesRegex(PreflightError, "inspection failed"),
        ):
            await inspect_fresh_rebuild_target(config(), identity(), 3)
        conn.close.assert_awaited_once()
