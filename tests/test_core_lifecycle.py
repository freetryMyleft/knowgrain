"""Deterministic proof and concurrency checks for pinned Core shutdown."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from lightrag.base import StoragesStatus
from lightrag.kg.postgres_impl import ClientManager

from knowgrain.config import Settings
from knowgrain.core_lifecycle import (
    CoreCallGate, STORAGE_ATTRIBUTES, VECTOR_ATTRIBUTES,
)
from knowgrain.lightrag_runtime import LightRAGRuntime


class FakeQueue:
    def __init__(self):
        self.stopped = False
        self.shutdown_count = 0
        self.stats = dict(queued=0, running=0, in_flight=0, worker_count=0, initialized=False)

    async def __call__(self, *args, **kwargs):
        return "{}"

    async def shutdown(self, *, graceful):
        assert graceful is False
        self.stopped = True
        self.shutdown_count += 1

    async def get_queue_stats(self):
        return self.stats


def install_close_protocol(rag=None, *, callback=None, embedding=None, on_finalize=None):
    """Upgrade an existing fake to all pinned fields; never change production checks."""
    rag = rag or SimpleNamespace()
    pool = SimpleNamespace(_closed=False, _closing=False, _holders=[])
    db = SimpleNamespace(pool=pool)
    registry = dict(db=db, ref_count=12, vector_signature={"vector_storage": "pgvector", "enable_vector": True})
    callback = callback or FakeQueue()
    embedding = embedding or FakeQueue()
    rag.role_llm_funcs = dict.fromkeys(("extract", "keyword", "query", "vlm"), callback)
    rag.embedding_func = SimpleNamespace(func=embedding)
    rag._storages_status = StoragesStatus.INITIALIZED
    rag._parser_executor = None
    rag._parser_shutdown_event = threading.Event()

    def shutdown_parser():
        rag._parser_shutdown_event.set()
        if rag._parser_executor is not None:
            rag._parser_executor.shutdown(wait=False, cancel_futures=True)
        rag._parser_executor = None
        rag._parser_shutdown_event = threading.Event()

    rag._shutdown_parser_executor = shutdown_parser
    for name in STORAGE_ATTRIBUTES:
        store = getattr(rag, name, None) or SimpleNamespace()
        setattr(rag, name, store)
        store.db = db

        async def finalize(store=store, name=name):
            if on_finalize is not None:
                await on_finalize(name)
            registry["ref_count"] -= 1
            if registry["ref_count"] == 0:
                pool._closed = True
                registry["db"] = None
                registry["vector_signature"] = None
            store.db = None

        store.finalize = AsyncMock(side_effect=finalize)
        if name in VECTOR_ATTRIBUTES:
            store._pending_vector_docs = {}
            store._pending_vector_deletes = set()

            async def flush(store=store):
                assert not getattr(embedding, "stopped", False), "flush requires live embedding"
                store._pending_vector_docs.clear()
                store._pending_vector_deletes.clear()

            store._flush_pending_vector_ops = AsyncMock(side_effect=flush)
    return rag, registry, pool


class CoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, **kwargs):
        return LightRAGRuntime(Settings(_env_file=None), **kwargs)

    def prepare(self, *, rag=None, callback=None, embedding=None, on_finalize=None, **kwargs):
        runtime = self.runtime(**kwargs)
        rag, registry, pool = install_close_protocol(
            rag, callback=callback, embedding=embedding, on_finalize=on_finalize,
        )
        runtime._rag = rag
        patcher = patch.object(ClientManager, "_instances", registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runtime, rag, registry, pool

    async def test_gate_counts_membership_and_closes_synchronously(self):
        gate = CoreCallGate()
        gate.enter()
        gate.enter()
        self.assertEqual(gate.count, 2)
        self.assertTrue(gate.owns_current_task())
        gate.close()
        with self.assertRaises(RuntimeError):
            gate.enter()
        gate.exit()
        self.assertFalse(gate.drained.is_set())
        gate.exit()
        self.assertTrue(gate.drained.is_set())
        gate.open()
        self.assertFalse(gate.closed)

    async def test_gate_rejects_another_loop(self):
        gate = CoreCallGate()
        gate.enter()
        gate.exit()

        def another_loop():
            async def enter():
                gate.enter()
            with self.assertRaisesRegex(RuntimeError, "owning event loop"):
                asyncio.run(enter())

        await asyncio.to_thread(another_loop)

    async def test_parallel_reads_drain_and_new_reads_rejected(self):
        started = asyncio.Event()
        release = asyncio.Event()
        n_started = 0

        async def query(*args, **kwargs):
            nonlocal n_started
            n_started += 1
            if n_started == 2:
                started.set()
            await release.wait()
            return {"data": {"chunks": []}}

        runtime, rag, _, _ = self.prepare(rag=SimpleNamespace(aquery_data=query))
        readers = [asyncio.create_task(runtime.retrieve("question")) for _ in range(2)]
        await started.wait()
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        self.assertFalse(runtime.is_ready)
        self.assertFalse(closing.done())
        with self.assertRaisesRegex(RuntimeError, "admission is closed"):
            await runtime.retrieve("new")
        rag.full_docs.finalize.assert_not_awaited()
        release.set()
        await asyncio.gather(*readers)
        await closing
        self.assertTrue(runtime.last_close_proof.succeeded)
        self.assertIsNone(runtime._rag)

    async def test_admitted_writes_waiting_lock_are_drained_without_deadlock(self):
        started, release = asyncio.Event(), asyncio.Event()
        inserts = []

        async def insert(text, **kwargs):
            inserts.append(text)
            if text == "first":
                started.set()
                await release.wait()

        runtime, rag, _, _ = self.prepare(rag=SimpleNamespace(ainsert=insert))
        rag.doc_status.get_by_id_strict = AsyncMock(return_value={"status": "processed"})
        first = asyncio.create_task(runtime.index_text(source_id="id", text="first", file_path="a"))
        await started.wait()
        second = asyncio.create_task(runtime.index_text(source_id="id", text="second", file_path="a"))
        await asyncio.sleep(0)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        self.assertEqual(runtime._call_gate.count, 2)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.gather(first, second, closing)
        self.assertEqual(inserts, ["first", "second"])
        self.assertEqual(runtime._call_gate.count, 0)

    async def test_every_public_core_entry_has_admission_guard(self):
        runtime = self.runtime()
        runtime._call_gate.close()
        calls = [
            runtime.index_text(source_id="id", text="text", file_path="a"),
            runtime.delete_revision(source_id="id"), runtime.inspect_revision(source_id="id"),
            runtime.retrieve("query"), runtime.entities_for_evidence([]),
            runtime.entity_chunk_ids("entity"), runtime.generate_json("system", "prompt"),
        ]
        for call in calls:
            with self.assertRaisesRegex(RuntimeError, "admission is closed"):
                await call
        self.assertEqual(runtime._call_gate.count, 0)

    async def test_flush_before_queue_shutdown_and_dedup_callbacks(self):
        same = FakeQueue()
        runtime, rag, _, _ = self.prepare(callback=same, embedding=same)
        rag.chunks_vdb._pending_vector_docs["id"] = object()
        rag.chunks_vdb._pending_vector_deletes.add("old")
        await runtime.close()
        self.assertEqual(same.shutdown_count, 1)
        self.assertTrue(runtime.last_close_proof.vectors_flushed)
        self.assertTrue(runtime.last_close_proof.queues_drained)
        self.assertEqual(rag._storages_status, StoragesStatus.FINALIZED)

    async def test_cancelled_real_llm_and_embedding_workers_stop_before_storages(self):
        from lightrag.llm_roles import _RoleLLMMixin
        from lightrag.utils import priority_limit_async_func_call

        started = [asyncio.Event(), asyncio.Event()]
        stopped = [asyncio.Event(), asyncio.Event()]

        async def provider(index, *args, **kwargs):
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped[index].set()

        async def llm(*args, **kwargs):
            return await provider(0, *args, **kwargs)

        async def embed(*args, **kwargs):
            return await provider(1, *args, **kwargs)

        llm_queue = _RoleLLMMixin._wrap_llm_role_func(
            SimpleNamespace(llm_response_cache=None), "query", llm, 1, 30, {},
        )
        embedding_queue = priority_limit_async_func_call(1, llm_timeout=30)(embed)

        async def finalized(name):
            self.assertTrue(all(event.is_set() for event in stopped))

        runtime, rag, _, _ = self.prepare(callback=llm_queue, embedding=embedding_queue,
                                           on_finalize=finalized)
        calls = [asyncio.create_task(runtime.generate_json("system", "prompt")),
                 asyncio.create_task(embedding_queue(["text"]))]
        try:
            await asyncio.gather(*(event.wait() for event in started))
            for call in calls:
                call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            await runtime.close()
            self.assertTrue(runtime.last_close_proof.succeeded)
            self.assertTrue(all(event.is_set() for event in stopped))
        finally:
            await llm_queue.shutdown(graceful=False)
            await embedding_queue.shutdown(graceful=False)

    async def test_queue_failure_never_releases_stores(self):
        for kind in ["missing", "active", "exception"]:
            runtime, rag, _, _ = self.prepare()
            callback = rag.embedding_func.func
            if kind == "missing":
                callback.get_queue_stats = None
            elif kind == "active":
                callback.stats["worker_count"] = 1
            else:
                callback.shutdown = AsyncMock(side_effect=RuntimeError("private api key"))
            with self.subTest(kind=kind), self.assertRaises(RuntimeError) as caught:
                await runtime.close()
            self.assertNotIn("private api key", str(caught.exception))
            rag.full_docs.finalize.assert_not_awaited()
            self.assertTrue(runtime.restart_required)
            self.assertIs(runtime._rag, rag)

    async def test_flush_failure_preserves_buffers_and_prevents_finalization(self):
        runtime, rag, _, _ = self.prepare()
        rag.chunks_vdb._pending_vector_docs["id"] = object()
        rag.chunks_vdb._flush_pending_vector_ops = AsyncMock(side_effect=RuntimeError("private text"))
        with self.assertRaises(RuntimeError) as caught:
            await runtime.close()
        self.assertNotIn("private text", str(caught.exception))
        self.assertIn("id", rag.chunks_vdb._pending_vector_docs)
        rag.full_docs.finalize.assert_not_awaited()
        self.assertFalse(runtime.last_close_proof.vectors_flushed)

    async def test_finalizer_failure_still_runs_remaining_and_fails_despite_zero_refs(self):
        runtime, rag, registry, pool = self.prepare()
        original = rag.full_docs.finalize

        async def failed():
            await original()
            raise ValueError("private credential")

        rag.full_docs.finalize = AsyncMock(side_effect=failed)
        with self.assertRaises(RuntimeError) as caught:
            await runtime.close()
        self.assertNotIn("private credential", str(caught.exception))
        for name in STORAGE_ATTRIBUTES:
            getattr(rag, name).finalize.assert_awaited_once()
        self.assertEqual(registry["ref_count"], 0)
        self.assertTrue(pool._closed)
        self.assertFalse(runtime.last_close_proof.succeeded)
        self.assertTrue(runtime.restart_required)
        self.assertIs(runtime._rag, rag)
        self.assertEqual(rag._storages_status, StoragesStatus.INITIALIZED)

    async def test_post_finalizer_release_must_be_strict(self):
        for kind in ["closing", "missing_pool", "negative_refs", "missing_refs", "buffer", "holder", "missing_holders"]:
            runtime, rag, registry, pool = self.prepare()
            finalizer = rag.doc_status.finalize

            async def corrupt_release():
                await finalizer()
                if kind == "closing":
                    pool._closed, pool._closing = False, True
                elif kind == "missing_pool":
                    del pool._closed
                elif kind == "negative_refs":
                    registry["ref_count"] = -1
                elif kind == "missing_refs":
                    del registry["ref_count"]
                elif kind == "buffer":
                    rag.chunks_vdb._pending_vector_docs["id"] = object()
                elif kind == "holder":
                    pool._holders = [SimpleNamespace(_con=SimpleNamespace(is_closed=lambda: False))]
                else:
                    del pool._holders

            rag.doc_status.finalize = AsyncMock(side_effect=corrupt_release)
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                await runtime.close()
            self.assertFalse(runtime.last_close_proof.succeeded)
            self.assertIs(runtime._rag, rag)

    async def test_missing_capture_protocol_does_not_finalize(self):
        for kind in ["storage", "db", "pool", "parser", "status"]:
            runtime, rag, _, _ = self.prepare()
            if kind == "storage":
                del rag.full_docs
            elif kind == "db":
                del rag.full_docs.db
            elif kind == "pool":
                del rag.full_docs.db.pool
            elif kind == "parser":
                del rag._parser_executor
            else:
                rag._storages_status = StoragesStatus.CREATED
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                await runtime.close()
            rag.doc_status.finalize.assert_not_awaited()

    async def test_preflight_rejects_external_ownership_or_missing_role(self):
        for kind in ["distinct", "external_db", "extra_owner", "missing_role", "signature", "holders"]:
            runtime, rag, registry, pool = self.prepare()
            if kind == "distinct":
                rag.text_chunks = rag.full_docs
            elif kind == "external_db":
                rag.full_docs.db = SimpleNamespace(pool=pool)
            elif kind == "extra_owner":
                registry["ref_count"] = 13
            elif kind == "missing_role":
                del rag.role_llm_funcs["vlm"]
            elif kind == "signature":
                registry["vector_signature"] = None
            else:
                del pool._holders
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                await runtime.close()
            rag.doc_status.finalize.assert_not_awaited()

    async def test_parser_executor_join_is_retained_until_running_thread_exits(self):
        runtime, rag, _, _ = self.prepare()
        executor = ThreadPoolExecutor(max_workers=1)
        release = threading.Event()
        started = threading.Event()
        event = rag._parser_shutdown_event

        def worker():
            started.set()
            release.wait()

        executor.submit(worker)
        await asyncio.to_thread(started.wait)
        rag._parser_executor = executor
        closing = asyncio.create_task(runtime.close())
        try:
            for _ in range(20):
                if event.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(event.is_set())
            self.assertFalse(closing.done())
            rag.full_docs.finalize.assert_not_awaited()
            release.set()
            await closing
            self.assertTrue(runtime.last_close_proof.parser_joined)
            self.assertFalse(any(thread.is_alive() for thread in executor._threads))
        finally:
            release.set()
            await asyncio.to_thread(executor.shutdown, wait=True)

    async def test_concurrent_close_coalesces_once(self):
        runtime, rag, _, _ = self.prepare()
        await asyncio.gather(*(runtime.close() for _ in range(5)))
        for name in STORAGE_ATTRIBUTES:
            getattr(rag, name).finalize.assert_awaited_once()
        retained = runtime._close_task
        await runtime.close()
        self.assertIs(runtime._close_task, retained)

    async def test_reentrant_close_rejected_before_gate_shutdown(self):
        runtime, rag, _, _ = self.prepare()

        async def query(*args, **kwargs):
            with self.assertRaisesRegex(RuntimeError, "admitted Core call"):
                await runtime.close()
            self.assertFalse(runtime._call_gate.closed)
            return {"data": {"chunks": []}}

        rag.aquery_data = query
        await runtime.retrieve("query")
        self.assertTrue(runtime.is_ready)
        await runtime.close()

    async def test_timeout_and_repeated_cancellation_keep_task_and_latch(self):
        for mode in ["timeout", "cancel"]:
            started, release = asyncio.Event(), asyncio.Event()
            runtime, rag, _, _ = self.prepare(close_timeout_seconds=0.03)

            async def query(*args, **kwargs):
                started.set()
                await release.wait()
                return {"data": {"chunks": []}}

            rag.aquery_data = query
            reader = asyncio.create_task(runtime.retrieve("query"))
            await started.wait()
            if mode == "timeout":
                with self.assertRaises(TimeoutError):
                    await runtime.close()
            else:
                for _ in range(2):
                    closing = asyncio.create_task(runtime.close())
                    await asyncio.sleep(0)
                    closing.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await closing
            retained = runtime._close_task
            self.assertFalse(retained.done())
            self.assertTrue(runtime.restart_required)
            release.set()
            await reader
            await runtime.close()
            self.assertIs(runtime._close_task, retained)
            self.assertTrue(runtime.last_close_proof.succeeded)
            self.assertTrue(runtime.restart_required)
            with self.assertRaisesRegex(RuntimeError, "restart"):
                await runtime.start()

    async def test_close_during_initialization_never_reopens_gate_and_new_epoch_invalidates_proof(self):
        runtime, first_rag, registry, _ = self.prepare()
        await runtime.close()
        old_proof = runtime.last_close_proof
        started, release = asyncio.Event(), asyncio.Event()
        next_rag, next_registry, _ = install_close_protocol()
        patcher = patch.object(ClientManager, "_instances", next_registry)
        patcher.start()
        self.addCleanup(patcher.stop)

        async def initialize():
            started.set()
            await release.wait()
            runtime._rag = next_rag

        runtime._start_unlocked = initialize
        starting = asyncio.create_task(runtime.start())
        await started.wait()
        self.assertIsNone(runtime.last_close_proof)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        release.set()
        await starting
        self.assertFalse(runtime.is_ready)
        await closing
        self.assertNotEqual(runtime.last_close_proof.epoch, old_proof.epoch)
        self.assertEqual(runtime.last_close_proof.core_id, id(next_rag))
        self.assertTrue(runtime.last_close_proof.succeeded)

    async def test_close_timeout_configuration_is_validated(self):
        for value in [True, 0, -1, "30", float("nan"), float("inf"), 10**1000]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.runtime(close_timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
