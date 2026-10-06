"""Real PostgreSQL ownership tests; coordinator witnesses are controlled fixtures."""

import asyncio
import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import delete, func, select, text, update

from knowgrain.config import Settings
from knowgrain.core_generation_repository import CoreGenerationRepository
from knowgrain.database import ApplicationDatabase
from knowgrain.index_fence import IndexConflict, LeaseLost, QuiescenceGuard, lock_index_fence
from knowgrain.index_identity import CoreIndexIdentity
from knowgrain.models import (
    CoreGeneration,
    CoreGenerationRevision,
    CoreSelector,
    GeneratedPage,
    GenerationJob,
    Job,
    RebuildItem,
    RebuildOperation,
    ReviewOperation,
    SourceDocument,
    SourceFileOperation,
    SourceRevision,
    VaultBinding,
    WikiPage,
)
from knowgrain.rebuild_repository import RebuildRepository
from knowgrain.source_repository import SourceRepository
from knowgrain.source_service import SourceService
from knowgrain.vault import VaultStore
from tests import test_index_profile


class ControlledQuiescence(QuiescenceGuard):
    def __init__(self, operation_id, generation_id, epoch):
        self.scope = (operation_id, generation_id, epoch)
        self.drained = True
        self.calls = 0

    def assert_quiescent(self, operation_id, old_generation_id, execution_epoch):
        self.calls += 1
        if not self.drained or self.scope != (operation_id, old_generation_id, execution_epoch):
            raise IndexConflict("Controlled coordinator has not drained this scope")


@unittest.skipUnless(
    os.environ.get("KNOWGRAIN_TEST_DATABASE") == "knowgrain_test",
    "disposable knowgrain_test DB not selected",
)
class RebuildPostgresTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        test_index_profile.IndexProfileTests.setUpClass()
        cls.addClassCleanup(test_index_profile.IndexProfileTests.doClassCleanups)
        cls.profile = test_index_profile.IndexProfileTests().capture()

    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        settings = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            knowgrain_postgres_db="knowgrain_test",
            vault_root=self.root / "vault",
            vault_parent_dir=self.root / "choices",
        )
        self.database = ApplicationDatabase(settings)
        self.addAsyncCleanup(self.database.close)
        self.assertTrue(await self.database.initialize(), self.database.last_error)
        async with self.database.session_factory() as session:
            if (
                await session.scalar(select(CoreGeneration.id).limit(1)) is not None
                or await session.get(CoreSelector, 1) is not None
                or await session.get(VaultBinding, 1) is not None
                or await session.scalar(select(SourceDocument.id).limit(1)) is not None
            ):
                self.skipTest("fixture must start without ledger/source/Vault rows")
        self.sources = []
        self.binding_id = uuid4()
        async with self.database.session_factory() as session, session.begin():
            session.add(
                VaultBinding(id=1, binding_id=self.binding_id, root_path=str(settings.vault_root))
            )
        self.addAsyncCleanup(self.cleanup_owned_rows)
        vault = VaultStore(settings.vault_root)
        vault.initialize()
        self.source_repository = SourceRepository(self.database)
        self.source_service = SourceService(settings, self.source_repository, vault)
        self.generations = CoreGenerationRepository(self.database)
        self.repository = RebuildRepository(self.database)
        self.identity = CoreIndexIdentity("legacy_" + uuid4().hex, self.root / "legacy")
        self.generation_ids = set()
        self.operation_ids = set()
        self.page_ids = set()
        self.generation_job_ids = set()
        self.review_ids = set()

    async def cleanup_owned_rows(self):
        async with self.database.session_factory() as session, session.begin():
            owned = await session.get(VaultBinding, 1)
            if owned is None or owned.binding_id != self.binding_id:
                return
            selector = await session.get(CoreSelector, 1)
            if selector is not None:
                selector.active_generation_id = selector.pending_rebuild_id = None
                await session.flush()
                await session.delete(selector)
            generation_ids = list(self.generation_ids)
            operation_ids = list(self.operation_ids)
            await session.execute(
                delete(RebuildItem).where(RebuildItem.operation_id.in_(operation_ids))
            )
            await session.execute(
                delete(CoreGenerationRevision).where(
                    CoreGenerationRevision.generation_id.in_(generation_ids)
                )
            )
            await session.execute(
                delete(RebuildOperation).where(RebuildOperation.id.in_(operation_ids))
            )
            await session.execute(
                delete(CoreGeneration).where(CoreGeneration.id.in_(generation_ids))
            )
            source_ids = [row.source_id for row in self.sources]
            revision_ids = select(SourceRevision.id).where(SourceRevision.source_id.in_(source_ids))
            await session.execute(
                update(SourceDocument)
                .where(SourceDocument.id.in_(source_ids))
                .values(current_revision_id=None, latest_revision_id=None)
            )
            await session.execute(
                delete(SourceFileOperation).where(SourceFileOperation.source_id.in_(source_ids))
            )
            await session.execute(delete(Job).where(Job.revision_id.in_(revision_ids)))
            await session.execute(
                delete(SourceRevision).where(SourceRevision.source_id.in_(source_ids))
            )
            await session.execute(delete(SourceDocument).where(SourceDocument.id.in_(source_ids)))
            await session.execute(
                delete(ReviewOperation).where(ReviewOperation.operation_id.in_(self.review_ids))
            )
            await session.execute(
                delete(GeneratedPage).where(GeneratedPage.page_id.in_(self.page_ids))
            )
            await session.execute(
                delete(GenerationJob).where(GenerationJob.id.in_(self.generation_job_ids))
            )
            await session.execute(delete(WikiPage).where(WikiPage.id.in_(self.page_ids)))
            await session.delete(owned)

    async def upload(self, name="source.md", content=b"Local evidence fixture", source_id=None):
        row = await self.source_service.import_file(name, content, source_id=source_id)
        self.sources.append(row)
        return row

    async def bootstrap(self):
        generation_id = await self.generations.bootstrap_legacy(self.identity)
        self.generation_ids.add(generation_id)
        return generation_id

    async def request(self, operation_id=None, expected=0):
        operation_id = operation_id or uuid4()
        result = await self.repository.request_rebuild(
            operation_id,
            expected_selector_version=expected,
            target_profile=self.profile,
            vault_binding_id=self.binding_id,
            working_parent=self.root / "generations",
        )
        self.operation_ids.add(operation_id)
        self.generation_ids.add(result["target_generation_id"])
        return result

    async def building(self):
        source = await self.upload()
        legacy = await self.bootstrap()
        operation = await self.request()
        parent = await self.repository.claim_operation(operation["operation_id"], uuid4())
        witness = ControlledQuiescence(operation["operation_id"], legacy, 0)
        snapshot = await self.repository.seal_snapshot(parent, witness)
        item = (await self.repository.list_items(operation["operation_id"]))[0]
        grant = await self.repository.claim_item(parent, item["item_id"], uuid4())
        return source, parent, snapshot, grant

    async def expire(self, model, key, *, future=False):
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(model)
                .where(model.id == key)
                .values(
                    lease_until=func.clock_timestamp() + text("INTERVAL '90 seconds'")
                    if future
                    else func.clock_timestamp() - text("INTERVAL '1 second'")
                )
            )

    async def wait_for_blocked(self, relation):
        async with asyncio.timeout(3):
            while True:
                async with self.database.engine.connect() as connection:
                    blocked = await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE :pattern AND pid <> pg_backend_pid())"
                        ),
                        {"pattern": "%FROM " + relation + "%"},
                    )
                if blocked:
                    return
                await asyncio.sleep(0.01)

    async def test_bootstrap_serializes_identity_and_preserves_legacy_evidence_metadata(self):
        source = await self.upload()
        async with self.database.session_factory() as session, session.begin():
            revision = await session.get(SourceRevision, source.revision_id)
            revision.index_state = "ready"
            revision.parsed_text_sha256 = "a" * 64
            revision.parsed_segments = [{"text": "retained", "page": 2}]
            revision.indexed_at = await session.scalar(select(func.clock_timestamp()))
            document = await session.get(SourceDocument, source.source_id)
            document.current_revision_id = source.revision_id
            original_time = revision.indexed_at
        results = await asyncio.gather(
            *(self.generations.bootstrap_legacy(self.identity) for _ in range(3))
        )
        self.generation_ids.update(results)
        self.assertEqual(len(set(results)), 1)
        async with self.database.session_factory() as session:
            generation = await session.get(CoreGeneration, results[0])
            member = await session.get(CoreGenerationRevision, (results[0], source.revision_id))
            revision = await session.get(SourceRevision, source.revision_id)
            self.assertEqual(generation.config_status, "legacy_unverified")
            self.assertIsNone(generation.canonical_profile)
            self.assertEqual(member.state, "write_intent")
            self.assertIsNone(member.verified_at)
            self.assertIsNone(member.physical_indexed_at)
            self.assertEqual(revision.indexed_at, original_time)
            self.assertEqual(revision.parsed_segments, [{"text": "retained", "page": 2}])
            self.assertIsNone(revision.indexed_generation_id)
        with self.assertRaises(IndexConflict):
            await self.generations.bootstrap_legacy(
                CoreIndexIdentity("different", self.root / "other")
            )

    async def test_request_cas_exact_replay_and_frozen_admission(self):
        legacy = await self.bootstrap()
        ids = [uuid4(), uuid4()]
        results = await asyncio.gather(*(self.request(key) for key in ids), return_exceptions=True)
        accepted = [row for row in results if isinstance(row, dict)]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(sum(isinstance(row, IndexConflict) for row in results), 1)
        operation = accepted[0]
        self.assertEqual(await self.request(operation["operation_id"]), operation)
        with self.assertRaises(IndexConflict):
            await self.request(operation["operation_id"], expected=1)
        async with self.database.session_factory() as session, session.begin():
            with self.assertRaises(IndexConflict):
                await lock_index_fence(session, purpose="admit")
            selector = await lock_index_fence(
                session, purpose="drain", generation_id=legacy, execution_epoch=0
            )
            self.assertEqual(selector.version, 1)
        public = str(await self.repository.status(operation["operation_id"]))
        self.assertNotIn("canonical_profile", public)
        self.assertNotIn(str(self.root), public)

    async def test_parent_same_owner_aba_retry_and_target_preservation(self):
        await self.bootstrap()
        operation = await self.request()
        owner = uuid4()
        first = await self.repository.claim_operation(operation["operation_id"], owner)
        await self.expire(RebuildOperation, operation["operation_id"])
        second = await self.repository.claim_operation(operation["operation_id"], owner)
        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertGreater(second.claim_fence, first.claim_fence)
        for call in (
            self.repository.renew_operation(first),
            self.repository.fail_operation(first, "core_failure"),
        ):
            with self.assertRaises(LeaseLost):
                await call
        await self.repository.renew_operation(second)
        await self.repository.fail_operation(second, "provider_unavailable")
        failed = await self.repository.status(operation["operation_id"])
        retried = await self.repository.retry(
            operation["operation_id"], expected_version=failed["version"]
        )
        third = await self.repository.claim_operation(operation["operation_id"], owner)
        repeated = await self.repository.retry(
            operation["operation_id"], expected_version=failed["version"]
        )
        self.assertEqual(repeated["target_generation_id"], retried["target_generation_id"])
        self.assertEqual(third.generation_id, first.generation_id)
        with self.assertRaises(IndexConflict):
            await self.repository.retry(operation["operation_id"], expected_version=0)

    async def test_snapshot_guard_running_rows_active_latest_and_idempotent_epoch(self):
        old = await self.upload(content=b"old fixture")
        latest = await self.upload(content=b"latest fixture", source_id=old.source_id)
        deleted = await self.upload(content=b"deleted fixture")
        async with self.database.session_factory() as session, session.begin():
            document = await session.get(SourceDocument, deleted.source_id)
            document.state = "deleted"
            active = await session.get(SourceDocument, old.source_id)
            active.current_revision_id = old.revision_id
            old_revision = await session.get(SourceRevision, old.revision_id)
            old_revision.parsed_text_sha256 = "b" * 64
            old_revision.indexed_at = await session.scalar(select(func.clock_timestamp()))
            original_time = old_revision.indexed_at
            session.add(
                SourceFileOperation(
                    source_id=deleted.source_id, lifecycle_version=0, kind="archive", manifest=[]
                )
            )
        legacy = await self.bootstrap()
        operation = await self.request()
        parent = await self.repository.claim_operation(operation["operation_id"], uuid4())
        guard = ControlledQuiescence(operation["operation_id"], legacy, 0)
        guard.drained = False
        with self.assertRaises(IndexConflict):
            await self.repository.seal_snapshot(parent, guard)
        guard.drained = True
        async with self.database.session_factory() as session, session.begin():
            job = await session.get(Job, latest.job_id)
            job.state = "running"
            job.lease_until = func.clock_timestamp() - text("INTERVAL '1 second'")
        with self.assertRaises(IndexConflict):
            await self.repository.seal_snapshot(parent, guard)
        async with self.database.session_factory() as session, session.begin():
            job = await session.get(Job, latest.job_id)
            job.state = "queued"
            active = await session.get(SourceDocument, old.source_id)
            active.latest_revision_id = None
        with self.assertRaises(IndexConflict):
            await self.repository.seal_snapshot(parent, guard)
        async with self.database.session_factory() as session, session.begin():
            active = await session.get(SourceDocument, old.source_id)
            active.latest_revision_id = latest.revision_id
        snapshot = await self.repository.seal_snapshot(parent, guard)
        self.assertEqual(await self.repository.seal_snapshot(parent, guard), snapshot)
        items = await self.repository.list_items(operation["operation_id"])
        self.assertEqual([row["revision_id"] for row in items], [latest.revision_id])
        async with self.database.session_factory() as session:
            selector = await session.get(CoreSelector, 1)
            self.assertEqual(selector.execution_epoch, 1)
            self.assertIsNone(
                (await session.get(SourceDocument, old.source_id)).current_revision_id
            )
            self.assertEqual(
                (await session.get(SourceRevision, old.revision_id)).indexed_at, original_time
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(SourceFileOperation)), 1
            )
        async with self.database.session_factory() as session, session.begin():
            with self.assertRaises(LeaseLost):
                await lock_index_fence(
                    session, purpose="drain", generation_id=legacy, execution_epoch=0
                )

    async def test_intent_precedes_manifest_and_partial_failure_is_retained(self):
        source, parent, _snapshot, grant = await self.building()
        with self.assertRaises(LeaseLost):
            await self.repository.record_manifest(grant, ["chunk-a"])
        digest = hashlib.sha256(b"parsed fixture").hexdigest()
        segments = [{"text": "parsed fixture", "page": None, "heading": "Topic"}]
        await self.repository.begin_write(grant, digest, segments)
        await self.repository.begin_write(grant, digest, segments)
        await self.repository.record_manifest(grant, ["chunk-a", "chunk-b", "chunk-a"])
        await self.repository.fail_item(grant, "core_failure")
        async with self.database.session_factory() as session:
            member = await session.get(
                CoreGenerationRevision, (parent.generation_id, source.revision_id)
            )
            self.assertEqual(member.state, "failed")
            self.assertEqual(member.cleanup_chunk_ids, ["chunk-a", "chunk-b"])
            self.assertIsNone(member.physical_indexed_at)
            self.assertIsNone(member.verified_at)
            self.assertIsNone(member.cleaned_at)
        again = await self.repository.claim_item(parent, grant.item_id, grant.owner)
        with self.assertRaises(LeaseLost):
            await self.repository.renew_item(grant)
        with self.assertRaises(IndexConflict):
            await self.repository.begin_write(again, digest, segments)

    async def test_parent_reclaim_invalidates_live_child(self):
        _source, parent, _snapshot, item = await self.building()
        await self.expire(RebuildOperation, parent.operation_id)
        replacement = await self.repository.claim_operation(parent.operation_id, parent.owner)
        with self.assertRaises(LeaseLost):
            await self.repository.renew_item(item)
        new_item = await self.repository.claim_item(replacement, item.item_id, item.owner)
        self.assertGreater(new_item.claim_fence, item.claim_fence)
        await self.repository.renew_item(new_item)
        await self.repository.begin_write(new_item, "c" * 64, [{"text": "fresh fixture"}])

    async def test_prepared_review_is_retained_without_blocking_snapshot(self):
        source = await self.upload()
        page_id, job_id, review_id = uuid4(), uuid4(), uuid4()
        self.page_ids.add(page_id)
        self.generation_job_ids.add(job_id)
        self.review_ids.add(review_id)
        page_path = self.root / "vault" / "Wiki" / "Drafts" / "retained.md"
        page_path.write_text("# Retained manual draft\n", encoding="utf-8")
        page_hash = hashlib.sha256(page_path.read_bytes()).hexdigest()
        original = self.root / "vault" / source.vault_path
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        async with self.database.session_factory() as session, session.begin():
            session.add(
                WikiPage(
                    id=page_id,
                    vault_path="Wiki/Drafts/retained.md",
                    title="Retained",
                    status="draft",
                    content_sha256=page_hash,
                )
            )
            session.add(
                GenerationJob(
                    id=job_id,
                    topic="Retained fixture",
                    output_page_id=page_id,
                    state="succeeded",
                    result={"fixture": True},
                )
            )
            await session.flush()
            session.add(
                GeneratedPage(
                    page_id=page_id,
                    generation_job_id=job_id,
                    draft={"fixture": True},
                    generated_sha256=page_hash,
                    model={"fixture": True},
                )
            )
            await session.flush()
            session.add(
                ReviewOperation(
                    operation_id=review_id,
                    page_id=page_id,
                    generation_page_id=page_id,
                    expected_page_sha256=page_hash,
                    expected_generation_sha256=page_hash,
                    reviewed_sha256=page_hash,
                    state="prepared",
                )
            )
        legacy = await self.bootstrap()
        operation = await self.request()
        grant = await self.repository.claim_operation(operation["operation_id"], uuid4())
        await self.repository.seal_snapshot(
            grant, ControlledQuiescence(grant.operation_id, legacy, 0)
        )
        async with self.database.session_factory() as session:
            review = await session.get(ReviewOperation, review_id)
            self.assertEqual(review.state, "prepared")
            self.assertIsNone(review.completed_at)
            self.assertEqual((await session.get(WikiPage, page_id)).content_sha256, page_hash)
        self.assertEqual(hashlib.sha256(page_path.read_bytes()).hexdigest(), page_hash)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), original_hash)

    async def test_target_profile_changes_block_item_intents(self):
        source, parent, _snapshot, grant = await self.building()
        async with self.database.session_factory() as session, session.begin():
            target = await session.get(CoreGeneration, parent.generation_id)
            target.graph_write_fingerprint = "f" * 64
        with self.assertRaises(IndexConflict):
            await self.repository.begin_write(grant, "c" * 64, [{"text": "profile drift fixture"}])
        async with self.database.session_factory() as session:
            self.assertIsNone(
                await session.get(
                    CoreGenerationRevision, (parent.generation_id, source.revision_id)
                )
            )

    async def test_empty_snapshot_is_explicit_and_sealed_once(self):
        legacy = await self.bootstrap()
        operation = await self.request()
        grant = await self.repository.claim_operation(operation["operation_id"], uuid4())
        guard = ControlledQuiescence(grant.operation_id, legacy, 0)
        snapshot = await self.repository.seal_snapshot(grant, guard)
        self.assertEqual(snapshot, hashlib.sha256(b"[]").hexdigest())
        self.assertEqual(await self.repository.seal_snapshot(grant, guard), snapshot)
        self.assertEqual(await self.repository.list_items(grant.operation_id), [])
        async with self.database.session_factory() as session:
            operation = await session.get(RebuildOperation, grant.operation_id)
            self.assertEqual(operation.snapshot_count, 0)
            self.assertEqual(operation.state, "building")
            self.assertEqual((await session.get(CoreSelector, 1)).execution_epoch, 1)

    async def test_item_from_other_operation_is_rejected(self):
        source, parent, snapshot, grant = await self.building()
        generation_id, operation_id, item_id = uuid4(), uuid4(), uuid4()
        self.generation_ids.add(generation_id)
        self.operation_ids.add(operation_id)
        async with self.database.session_factory() as session, session.begin():
            session.add(
                CoreGeneration(
                    id=generation_id,
                    workspace="other_" + generation_id.hex,
                    working_dir=str(self.root / generation_id.hex),
                    config_status="legacy_unverified",
                )
            )
            await session.flush()
            session.add(
                RebuildOperation(
                    id=operation_id,
                    target_generation_id=generation_id,
                    expected_selector_version=0,
                    vault_binding_id=self.binding_id,
                    vault_root_path=str(self.root / "vault"),
                    request_sha256="e" * 64,
                )
            )
            await session.flush()
            session.add(
                RebuildItem(
                    id=item_id,
                    operation_id=operation_id,
                    generation_id=generation_id,
                    source_id=source.source_id,
                    revision_id=source.revision_id,
                    lifecycle_version=0,
                    filename="source.md",
                    media_type="text/markdown",
                    vault_path=source.vault_path,
                    source_sha256=hashlib.sha256(b"Local evidence fixture").hexdigest(),
                    snapshot_sha256=snapshot,
                )
            )
        with self.assertRaises(IndexConflict):
            await self.repository.claim_item(parent, item_id, grant.owner)
        await self.repository.renew_item(grant)

    async def test_source_lock_wait_expiry_blocks_snapshot_commit(self):
        source = await self.upload()
        legacy = await self.bootstrap()
        operation = await self.request()
        parent = await self.repository.claim_operation(operation["operation_id"], uuid4())
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                update(RebuildOperation)
                .where(RebuildOperation.id == parent.operation_id)
                .values(lease_until=func.clock_timestamp() + text("INTERVAL '0.25 seconds'"))
            )
        async with self.database.session_factory() as blocker, blocker.begin():
            await blocker.execute(
                select(SourceDocument)
                .where(SourceDocument.id == source.source_id)
                .with_for_update()
            )
            task = asyncio.create_task(
                self.repository.seal_snapshot(
                    parent, ControlledQuiescence(parent.operation_id, legacy, 0)
                )
            )
            await self.wait_for_blocked("source_document")
            await asyncio.sleep(0.3)
        with self.assertRaises(LeaseLost):
            await task
        async with self.database.session_factory() as session:
            self.assertEqual((await session.get(CoreSelector, 1)).execution_epoch, 0)
            self.assertIsNone(
                (await session.get(RebuildOperation, parent.operation_id)).snapshot_sha256
            )

    async def test_expiry_after_source_item_and_member_lock_waits(self):
        source, parent, _snapshot, grant = await self.building()
        await self.repository.begin_write(grant, "d" * 64, [{"text": "lock fixture"}])
        cases = (
            (SourceDocument, SourceDocument.id == source.source_id, "source_document"),
            (RebuildItem, RebuildItem.id == grant.item_id, "rebuild_item"),
            (
                CoreGenerationRevision,
                (CoreGenerationRevision.generation_id == parent.generation_id)
                & (CoreGenerationRevision.revision_id == source.revision_id),
                "core_generation_revision",
            ),
        )
        for model, condition, relation in cases:
            with self.subTest(relation=relation):
                async with self.database.session_factory() as session, session.begin():
                    await session.execute(
                        update(RebuildItem)
                        .where(RebuildItem.id == grant.item_id)
                        .values(
                            lease_until=func.clock_timestamp() + text("INTERVAL '0.25 seconds'")
                        )
                    )
                async with self.database.session_factory() as blocker, blocker.begin():
                    await blocker.execute(select(model).where(condition).with_for_update())
                    task = asyncio.create_task(
                        self.repository.record_manifest(grant, ["late-chunk"])
                    )
                    await self.wait_for_blocked(relation)
                    await asyncio.sleep(0.3)
                with self.assertRaises(LeaseLost):
                    await task
        async with self.database.session_factory() as session:
            member = await session.get(
                CoreGenerationRevision, (parent.generation_id, source.revision_id)
            )
            self.assertIsNone(member.cleanup_chunk_ids)

    async def test_profile_drift_and_restart_latch_fail_closed(self):
        await self.bootstrap()
        operation = await self.request()
        async with self.database.session_factory() as session, session.begin():
            generation = await session.get(CoreGeneration, operation["target_generation_id"])
            generation.snapshot_fingerprint = "f" * 64
        with self.assertRaises(IndexConflict):
            await self.repository.claim_operation(operation["operation_id"], uuid4())
        async with self.database.session_factory() as session, session.begin():
            generation = await session.get(CoreGeneration, operation["target_generation_id"])
            generation.snapshot_fingerprint = self.profile.snapshot_fingerprint
        grant = await self.repository.claim_operation(operation["operation_id"], uuid4())
        await self.repository.fail_operation(grant, "restart_required", restart_required=True)
        with self.assertRaises(IndexConflict):
            await self.repository.retry(
                operation["operation_id"],
                expected_version=(await self.repository.status(operation["operation_id"]))[
                    "version"
                ],
            )
        async with self.database.session_factory() as session:
            selector = await session.get(CoreSelector, 1)
            self.assertTrue(selector.frozen)
            self.assertTrue(selector.restart_required)


if __name__ == "__main__":
    unittest.main()
