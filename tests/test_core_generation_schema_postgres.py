"""Inert ledger constraints on an explicitly selected disposable PostgreSQL DB."""

from __future__ import annotations

import os
import runpy
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from knowgrain.models import (
    Base,
    CoreGeneration,
    CoreGenerationRevision,
    CoreSelector,
    Job,
    RebuildItem,
    RebuildOperation,
    SourceDocument,
    SourceRevision,
)


@unittest.skipUnless(
    os.environ.get("KNOWGRAIN_TEST_DATABASE") == "knowgrain_test",
    "disposable knowgrain_test DB not selected",
)
class CoreGenerationSchemaPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        port = int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432"))
        self.engine = create_async_engine(
            f"postgresql+asyncpg://knowgrain:knowgrain-local@127.0.0.1:{port}/knowgrain_test"
        )
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.assertEqual(
            await self.connection.scalar(text("SELECT version_num FROM alembic_version")),
            "0013_core_generations",
        )

    async def asyncTearDown(self):
        await self.transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def rejected(self, model, **values):
        async with self.connection.begin_nested() as nested:
            with self.assertRaises(IntegrityError):
                await self.connection.execute(insert(model).values(**values))
            await nested.rollback()

    async def generation(self, **overrides):
        identity = uuid4()
        values = {
            "id": identity,
            "workspace": f"ledger-test-{identity}",
            "working_dir": f"/tmp/ledger-test/{identity}",
            "config_status": "legacy_unverified",
        }
        values.update(overrides)
        await self.connection.execute(insert(CoreGeneration).values(**values))
        return identity

    async def source(self):
        source_id, revision_id = uuid4(), uuid4()
        await self.connection.execute(
            insert(SourceDocument).values(id=source_id, filename="ledger-test.txt")
        )
        await self.connection.execute(
            insert(SourceRevision).values(
                id=revision_id,
                source_id=source_id,
                filename="ledger-test.txt",
                sha256="a" * 64,
                vault_path=f"Sources/Files/{source_id}/{revision_id}.txt",
                media_type="text/plain",
            )
        )
        return source_id, revision_id

    async def operation(self, target=None):
        target = target or await self.generation()
        operation = {
            "id": uuid4(),
            "target_generation_id": target,
            "expected_selector_version": 0,
            "vault_binding_id": uuid4(),
            "vault_root_path": "/tmp/ledger-test/vault",
            "request_sha256": "b" * 64,
        }
        await self.connection.execute(insert(RebuildOperation).values(**operation))
        return operation

    async def test_sealed_profile_and_namespace_uniqueness(self):
        sealed = {
            "id": uuid4(),
            "workspace": f"sealed-{uuid4()}",
            "working_dir": f"/tmp/{uuid4()}",
            "config_status": "sealed",
            "vector_model_name": "kg_" + uuid4().hex[:24],
            "canonical_profile": "{}",
            "content_embedding_fingerprint": "a" * 64,
            "graph_write_fingerprint": "b" * 64,
            "llm_fingerprint": "c" * 64,
            "snapshot_fingerprint": "d" * 64,
        }
        await self.connection.execute(insert(CoreGeneration).values(**sealed))
        for field, invalid in (
            ("vector_model_name", None),
            ("vector_model_name", "kg_bad"),
            ("canonical_profile", None),
            ("snapshot_fingerprint", "z" * 64),
            ("config_status", "legacy_unverified"),
        ):
            await self.rejected(CoreGeneration, **(sealed | {"id": uuid4(), field: invalid}))
        for field in ("workspace", "working_dir", "vector_model_name"):
            duplicate = sealed | {
                "id": uuid4(),
                "workspace": f"sealed-{uuid4()}",
                "working_dir": f"/tmp/{uuid4()}",
                "vector_model_name": "kg_" + uuid4().hex[:24],
            }
            duplicate[field] = sealed[field]
            await self.rejected(CoreGeneration, **duplicate)

    async def test_operation_snapshot_leases_and_target_ownership(self):
        operation = await self.operation()
        for invalid in (
            {"target_generation_id": operation["target_generation_id"]},
            {
                "old_generation_id": operation["target_generation_id"],
                "target_generation_id": operation["target_generation_id"],
            },
            {"expected_selector_version": -1},
            {"retry_from_version": -1},
            {"snapshot_sha256": "a" * 64},
            {"snapshot_count": 0},
            {"snapshot_sha256": "a" * 64, "snapshot_count": 0, "snapshot_execution_epoch": -1},
            {"claim_token": uuid4()},
            {"lease_owner": uuid4()},
            {"claim_fence": -1},
            {"request_sha256": "X" * 64},
        ):
            values = (
                operation
                | {"id": uuid4(), "target_generation_id": await self.generation()}
                | invalid
            )
            await self.rejected(RebuildOperation, **values)

    async def test_selector_singleton_frozen_and_counters(self):
        operation = await self.operation()
        await self.rejected(CoreSelector, id=2)
        await self.rejected(CoreSelector, id=1, version=-1)
        await self.rejected(CoreSelector, id=1, execution_epoch=-1)
        await self.rejected(CoreSelector, id=1, pending_rebuild_id=operation["id"], frozen=False)
        # Frozen is valid without a pending operation, e.g. restart-required latch.
        await self.connection.execute(insert(CoreSelector).values(id=1, frozen=True))
        await self.rejected(CoreSelector, id=1)

    async def test_item_exact_revision_target_and_claim_groups(self):
        operation = await self.operation()
        source_id, revision_id = await self.source()
        other_source, _ = await self.source()
        item = {
            "id": uuid4(),
            "operation_id": operation["id"],
            "generation_id": operation["target_generation_id"],
            "source_id": source_id,
            "revision_id": revision_id,
            "lifecycle_version": 0,
            "filename": "ledger-test.txt",
            "media_type": "text/plain",
            "vault_path": "Sources/Files/ledger-test.txt",
            "source_sha256": "a" * 64,
            "snapshot_sha256": "b" * 64,
        }
        for invalid in (
            {"source_id": other_source},
            {"generation_id": await self.generation()},
            {"lifecycle_version": -1},
            {"snapshot_sha256": "bad"},
            {"parent_claim_token": uuid4()},
            {"parent_claim_fence": -1},
            {"state": "verified"},
            {"claim_token": uuid4(), "lease_owner": uuid4(), "lease_until": datetime.now(UTC)},
        ):
            await self.rejected(RebuildItem, **(item | invalid))
        await self.connection.execute(insert(RebuildItem).values(**item))
        await self.rejected(RebuildItem, **(item | {"id": uuid4()}))

    async def test_members_record_intent_and_reject_false_verification(self):
        generation = await self.generation()
        source_id, revision_id = await self.source()
        other_source, _ = await self.source()
        member = {"generation_id": generation, "source_id": source_id, "revision_id": revision_id}
        await self.rejected(CoreGenerationRevision, **(member | {"source_id": other_source}))
        await self.rejected(CoreGenerationRevision, **(member | {"state": "verified"}))
        await self.rejected(CoreGenerationRevision, **(member | {"state": "cleaned"}))
        await self.rejected(CoreGenerationRevision, **(member | {"claim_fence": -1}))
        await self.connection.execute(insert(CoreGenerationRevision).values(**member))
        await self.rejected(CoreGenerationRevision, **member)
        row = (
            (
                await self.connection.execute(
                    select(CoreGenerationRevision.__table__).where(
                        CoreGenerationRevision.generation_id == generation
                    )
                )
            )
            .mappings()
            .one()
        )
        self.assertEqual(row["state"], "write_intent")
        self.assertIsNone(row["verified_at"])
        self.assertIsNotNone(row["write_intent_at"])
        await self.connection.execute(insert(Job).values(revision_id=revision_id))
        task = (
            (
                await self.connection.execute(
                    select(Job.__table__).where(Job.revision_id == revision_id)
                )
            )
            .mappings()
            .one()
        )
        self.assertIsNone(task["generation_id"])
        self.assertIsNone(task["execution_epoch"])
        self.assertIsNone(task["claim_token"])
        self.assertEqual(task["claim_fence"], 0)

    async def test_orm_schema_parity_and_named_checks(self):
        def compare(connection):
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            self.assertEqual(compare_metadata(context, Base.metadata), [])
            inspector = inspect(connection)
            for table in Base.metadata.tables.values():
                expected = {
                    constraint.name
                    for constraint in table.constraints
                    if constraint.__class__.__name__ == "CheckConstraint"
                }
                actual = {
                    constraint["name"] for constraint in inspector.get_check_constraints(table.name)
                }
                self.assertTrue(expected <= actual, (table.name, expected - actual))

        await self.connection.run_sync(compare)

    async def test_populated_downgrade_refuses_before_schema_mutation(self):
        generation = await self.generation()
        source_id, revision_id = await self.source()
        await self.connection.execute(
            insert(CoreGenerationRevision).values(
                generation_id=generation, source_id=source_id, revision_id=revision_id
            )
        )
        migration = runpy.run_path(
            str(Path(__file__).parents[1] / "infra/migrations/versions/0013_core_generations.py")
        )

        def refuse(connection):
            with (
                Operations.context(MigrationContext.configure(connection)),
                self.assertRaisesRegex(RuntimeError, "populated Core generation ledgers"),
            ):
                migration["downgrade"]()

        await self.connection.run_sync(refuse)
        self.assertEqual(
            await self.connection.scalar(
                select(CoreGenerationRevision.state).where(
                    CoreGenerationRevision.generation_id == generation
                )
            ),
            "write_intent",
        )
        await self.connection.execute(select(Job.generation_id, Job.claim_fence))

    async def test_new_foreign_keys_have_leading_reverse_lookup_indexes(self):
        required = {
            "core_generation_revision": [("revision_id", "source_id"), ("generation_id",)],
            "rebuild_item": [("revision_id", "source_id"), ("operation_id", "generation_id")],
            "rebuild_operation": [("old_generation_id",), ("target_generation_id",)],
            "core_selector": [("active_generation_id",), ("pending_rebuild_id",)],
            "source_revision": [("indexed_generation_id",)],
            "job": [("generation_id",)],
            "core_maintenance_job": [("generation_id",)],
            "source_file_operation": [("generation_id",)],
            "generation_job": [("generation_id",)],
            "query_job": [("generation_id",)],
        }

        def assert_coverage(connection):
            inspector = inspect(connection)
            for table, foreign_keys in required.items():
                indexes = [tuple(index["column_names"]) for index in inspector.get_indexes(table)]
                indexes.extend(
                    tuple(unique["column_names"])
                    for unique in inspector.get_unique_constraints(table)
                )
                indexes.append(tuple(inspector.get_pk_constraint(table)["constrained_columns"]))
                for columns in foreign_keys:
                    self.assertTrue(
                        any(index[: len(columns)] == columns for index in indexes),
                        (table, columns),
                    )

        await self.connection.run_sync(assert_coverage)

    async def test_downgrade_preserves_unbound_task_execution_metadata(self):
        _, revision_id = await self.source()
        migration = runpy.run_path(
            str(Path(__file__).parents[1] / "infra/migrations/versions/0013_core_generations.py")
        )
        for metadata in (
            {"execution_epoch": 0},
            {"claim_token": uuid4()},
            {"claim_fence": 1},
            {"retired_at": datetime.now(UTC)},
            {"retired_reason": "retired old execution"},
        ):
            async with self.connection.begin_nested() as nested:
                await self.connection.execute(
                    insert(Job).values(revision_id=revision_id, **metadata)
                )

                def refuse(connection):
                    with (
                        Operations.context(MigrationContext.configure(connection)),
                        self.assertRaisesRegex(RuntimeError, "tasks with execution metadata"),
                    ):
                        migration["downgrade"]()

                await self.connection.run_sync(refuse)
                await self.connection.execute(select(Job.generation_id, Job.claim_fence))
                await nested.rollback()
