import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from knowgrain.config import Settings
from knowgrain.lightrag_runtime import LightRAGRuntime


class StrictStore:
    def __init__(self, records=None):
        self.records = dict(records or {})

    async def get_by_id_strict(self, key):
        return self.records.get(key)

    async def get_by_ids(self, keys):
        return [self.records.get(key) for key in keys]


class CoreMaintenanceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.revision_id = str(uuid4())
        self.chunk_id = "chunk-one"
        self.stores = {
            "doc_status": StrictStore({
                self.revision_id: {"status": "processed", "chunks_list": [self.chunk_id]},
            }),
            "full_docs": StrictStore({self.revision_id: {"content": "source"}}),
            "full_entities": StrictStore(),
            "full_relations": StrictStore(),
            "text_chunks": StrictStore({
                self.chunk_id: {"id": self.chunk_id, "full_doc_id": self.revision_id},
            }),
        }
        self.rag = SimpleNamespace(**self.stores)
        self.rag.adelete_by_doc_id = AsyncMock(side_effect=self._delete_everything)
        self.runtime = LightRAGRuntime(Settings(_env_file=None))
        self.runtime._rag = self.rag

    async def _delete_everything(self, doc_id, *, delete_llm_cache=False):
        for name in ("doc_status", "full_docs", "full_entities", "full_relations"):
            self.stores[name].records.pop(doc_id, None)
        for chunk_id in tuple(self.stores["text_chunks"].records):
            if self.stores["text_chunks"].records[chunk_id].get("full_doc_id") == doc_id:
                self.stores["text_chunks"].records.pop(chunk_id, None)
        return SimpleNamespace(doc_id=doc_id, status="success")

    async def delete_revision(self, **kwargs):
        kwargs.setdefault("persist_manifest", AsyncMock())
        return await self.runtime.delete_revision(**kwargs)

    async def test_persists_complete_owned_manifest_before_public_delete(self):
        prior_chunk_id = "chunk-from-previous-attempt"
        self.stores["text_chunks"].records[prior_chunk_id] = {
            "id": prior_chunk_id,
            "full_doc_id": self.revision_id,
        }
        persisted = AsyncMock()
        events = []

        async def record_manifest(chunk_ids):
            events.append(("manifest", chunk_ids))
            await persisted(chunk_ids)

        async def delete(doc_id, *, delete_llm_cache=False):
            events.append(("delete", doc_id, delete_llm_cache))
            return await self._delete_everything(doc_id, delete_llm_cache=delete_llm_cache)

        self.rag.adelete_by_doc_id.side_effect = delete
        await self.delete_revision(
            source_id=self.revision_id,
            expected_chunk_ids=[prior_chunk_id],
            persist_manifest=record_manifest,
            delete_llm_cache=True,
        )

        expected_manifest = tuple(sorted([self.chunk_id, prior_chunk_id]))
        persisted.assert_awaited_once_with(expected_manifest)
        self.assertEqual(events[0], ("manifest", expected_manifest))
        self.assertEqual(events[1], ("delete", self.revision_id, True))

    async def test_foreign_chunk_membership_fails_before_persistence_or_delete(self):
        self.stores["text_chunks"].records[self.chunk_id]["full_doc_id"] = str(uuid4())
        persist = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "foreign text chunk"):
            await self.delete_revision(
                source_id=self.revision_id,
                persist_manifest=persist,
            )

        persist.assert_not_awaited()
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_insert_rechecks_owner_after_waiting_for_write_lock(self):
        self.rag.ainsert = AsyncMock()
        validate_owner = AsyncMock()
        await self.runtime._write_lock.acquire()
        insertion = asyncio.create_task(self.runtime.index_text(
            source_id=self.revision_id, text="synthetic", file_path="proof.txt",
            before_insert=validate_owner,
        ))
        await asyncio.sleep(0)
        validate_owner.assert_not_awaited()
        validate_owner.side_effect = RuntimeError("lease revoked during writer wait")
        self.runtime._write_lock.release()
        with self.assertRaisesRegex(RuntimeError, "lease revoked"):
            await insertion
        validate_owner.assert_awaited_once()
        self.rag.ainsert.assert_not_awaited()

    async def test_expected_manifest_with_foreign_chunk_fails_closed(self):
        foreign_id = "foreign-chunk"
        self.stores["text_chunks"].records[foreign_id] = {
            "id": foreign_id,
            "full_doc_id": str(uuid4()),
        }
        with self.assertRaisesRegex(RuntimeError, "foreign text chunk"):
            await self.delete_revision(
                source_id=self.revision_id,
                expected_chunk_ids=[foreign_id],
            )
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_manifest_callback_failure_prevents_core_deletion(self):
        persist = AsyncMock(side_effect=RuntimeError("lease expired"))
        with self.assertRaisesRegex(RuntimeError, "lease expired"):
            await self.delete_revision(
                source_id=self.revision_id,
                persist_manifest=persist,
            )
        persist.assert_awaited_once()
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_persist_callback_is_required_before_deleting_known_data(self):
        with self.assertRaisesRegex(RuntimeError, "persistence is required"):
            await self.runtime.delete_revision(source_id=self.revision_id)
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_persist_callback_is_required_for_nonempty_saved_manifest(self):
        self.stores["doc_status"].records.clear()
        self.stores["full_docs"].records.clear()
        self.stores["text_chunks"].records.clear()
        with self.assertRaisesRegex(RuntimeError, "persistence is required"):
            await self.runtime.delete_revision(
                source_id=self.revision_id,
                expected_chunk_ids=["previously-owned-chunk"],
            )
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_rejects_noncanonical_revision_uuid_before_core_access(self):
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            await self.delete_revision(source_id=self.revision_id.upper())
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_rejects_unbounded_cleanup_manifest_before_core_access(self):
        too_many = [f"chunk-{index}" for index in range(10_001)]
        with self.assertRaisesRegex(RuntimeError, "manifest exceeds the supported limit"):
            await self.delete_revision(
                source_id=self.revision_id,
                expected_chunk_ids=too_many,
            )
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_not_allowed_and_fail_results_are_retryable_errors(self):
        for status in ("not_allowed", "fail"):
            with self.subTest(status=status):
                self.rag.adelete_by_doc_id.reset_mock(side_effect=True)
                self.rag.adelete_by_doc_id.side_effect = None
                self.rag.adelete_by_doc_id.return_value = SimpleNamespace(
                    doc_id=self.revision_id,
                    status=status,
                    message="upstream private detail",
                )
                with self.assertRaisesRegex(RuntimeError, "refused or failed") as raised:
                    await self.delete_revision(source_id=self.revision_id)
                self.assertNotIn("upstream private detail", str(raised.exception))

    async def test_core_exception_is_sanitized_and_propagated_as_retryable(self):
        self.rag.adelete_by_doc_id.side_effect = RuntimeError("private upstream detail")
        with self.assertRaisesRegex(RuntimeError, "LightRAG document deletion failed") as raised:
            await self.delete_revision(source_id=self.revision_id)
        self.assertNotIn("private upstream detail", str(raised.exception))

    async def test_malformed_or_wrong_document_result_is_rejected(self):
        malformed_results = [
            object(),
            SimpleNamespace(doc_id=str(uuid4()), status="success"),
            SimpleNamespace(doc_id=self.revision_id, status="unknown"),
        ]
        for result in malformed_results:
            with self.subTest(result=result):
                self.rag.adelete_by_doc_id.reset_mock(side_effect=True)
                self.rag.adelete_by_doc_id.side_effect = None
                self.rag.adelete_by_doc_id.return_value = result
                with self.assertRaisesRegex(RuntimeError, "malformed document deletion result"):
                    await self.delete_revision(source_id=self.revision_id)

    async def test_not_found_with_leftover_data_is_not_reported_as_clean(self):
        self.rag.adelete_by_doc_id.side_effect = None
        self.rag.adelete_by_doc_id.return_value = SimpleNamespace(
            doc_id=self.revision_id,
            status="not_found",
        )
        with self.assertRaisesRegex(RuntimeError, "left document data behind"):
            await self.delete_revision(source_id=self.revision_id)

    async def test_not_found_with_no_residuals_is_idempotent_success(self):
        self.stores["doc_status"].records.clear()
        self.stores["full_docs"].records.clear()
        self.stores["text_chunks"].records.clear()
        self.rag.adelete_by_doc_id.side_effect = None
        self.rag.adelete_by_doc_id.return_value = SimpleNamespace(
            doc_id=self.revision_id,
            status="not_found",
        )
        await self.runtime.delete_revision(source_id=self.revision_id)
        self.rag.adelete_by_doc_id.assert_awaited_once_with(
            self.revision_id,
            delete_llm_cache=False,
        )

    async def test_missing_status_with_orphan_document_data_fails_before_delete(self):
        self.stores["doc_status"].records.clear()
        with self.assertRaisesRegex(RuntimeError, "status is missing while document data remains"):
            await self.delete_revision(source_id=self.revision_id)
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_missing_status_with_owned_orphan_chunk_fails_before_delete(self):
        self.stores["doc_status"].records.clear()
        self.stores["full_docs"].records.clear()
        with self.assertRaisesRegex(
            RuntimeError, "status is missing while owned text chunks remain"
        ):
            await self.delete_revision(
                source_id=self.revision_id,
                expected_chunk_ids=[self.chunk_id],
            )
        self.rag.adelete_by_doc_id.assert_not_awaited()

    async def test_postconditions_check_all_document_stores(self):
        for residual_store in ("doc_status", "full_docs", "full_entities", "full_relations"):
            with self.subTest(store=residual_store):
                self.stores["doc_status"].records[self.revision_id] = {
                    "status": "processed",
                    "chunks_list": [self.chunk_id],
                }
                for name in ("full_docs", "full_entities", "full_relations"):
                    self.stores[name].records.pop(self.revision_id, None)
                self.stores[residual_store].records[self.revision_id] = (
                    {"status": "processed", "chunks_list": [self.chunk_id]}
                    if residual_store == "doc_status"
                    else {"marker": True}
                )
                self.stores["text_chunks"].records.pop(self.chunk_id, None)

                async def leave_one(doc_id, *, delete_llm_cache=False):
                    for name in ("doc_status", "full_docs", "full_entities", "full_relations"):
                        if name != residual_store:
                            self.stores[name].records.pop(doc_id, None)
                    self.stores["text_chunks"].records.pop(self.chunk_id, None)
                    return SimpleNamespace(doc_id=doc_id, status="success")

                self.rag.adelete_by_doc_id.reset_mock(side_effect=True)
                self.rag.adelete_by_doc_id.side_effect = leave_one
                with self.assertRaisesRegex(RuntimeError, "left document data behind"):
                    await self.delete_revision(source_id=self.revision_id)

    async def test_postconditions_check_owned_text_chunks(self):
        async def leave_chunk(doc_id, *, delete_llm_cache=False):
            for name in ("doc_status", "full_docs", "full_entities", "full_relations"):
                self.stores[name].records.pop(doc_id, None)
            return SimpleNamespace(doc_id=doc_id, status="success")

        self.rag.adelete_by_doc_id.side_effect = leave_chunk
        with self.assertRaisesRegex(RuntimeError, "left owned text chunks behind"):
            await self.delete_revision(source_id=self.revision_id)

    async def test_write_lock_serializes_index_and_cleanup(self):
        index_started = asyncio.Event()
        release_index = asyncio.Event()
        delete_started = asyncio.Event()

        async def index(text, *, ids, file_paths):
            index_started.set()
            self.stores["doc_status"].records[self.revision_id] = {
                "status": "processed",
                "chunks_list": [],
            }
            await release_index.wait()

        async def delete(doc_id, *, delete_llm_cache=False):
            delete_started.set()
            return await self._delete_everything(doc_id, delete_llm_cache=delete_llm_cache)

        self.rag.ainsert = index
        self.rag.adelete_by_doc_id.side_effect = delete
        indexing = asyncio.create_task(
            self.runtime.index_text(
                source_id=self.revision_id,
                text="evidence",
                file_path="source.txt",
            )
        )
        await index_started.wait()
        deleting = asyncio.create_task(self.delete_revision(source_id=self.revision_id))
        await asyncio.sleep(0)
        self.assertFalse(delete_started.is_set())
        release_index.set()
        await indexing
        await deleting
        self.assertTrue(delete_started.is_set())

    async def test_cancellation_drains_core_delete_before_releasing_writer_lock(self):
        delete_started = asyncio.Event()
        finish_delete = asyncio.Event()
        index_started = asyncio.Event()

        async def delete(doc_id, *, delete_llm_cache=False):
            delete_started.set()
            await finish_delete.wait()
            return await self._delete_everything(doc_id, delete_llm_cache=delete_llm_cache)

        async def index(text, *, ids, file_paths):
            index_started.set()
            self.stores["doc_status"].records[self.revision_id] = {
                "status": "processed",
                "chunks_list": [],
            }

        self.rag.adelete_by_doc_id.side_effect = delete
        self.rag.ainsert = index
        deleting = asyncio.create_task(self.delete_revision(source_id=self.revision_id))
        await delete_started.wait()
        deleting.cancel()
        await asyncio.sleep(0)
        indexing = asyncio.create_task(
            self.runtime.index_text(source_id=self.revision_id, text="new", file_path="source.txt")
        )
        await asyncio.sleep(0)
        self.assertFalse(index_started.is_set())
        self.assertFalse(deleting.done())

        finish_delete.set()
        with self.assertRaises(asyncio.CancelledError):
            await deleting
        await indexing
        self.assertTrue(index_started.is_set())

    async def test_close_waits_for_active_write_before_finalizing_storages(self):
        index_started = asyncio.Event()
        release_index = asyncio.Event()
        finalized = asyncio.Event()

        async def index(text, *, ids, file_paths):
            index_started.set()
            await release_index.wait()
            self.stores["doc_status"].records[self.revision_id] = {
                "status": "processed",
                "chunks_list": [],
            }

        async def finalize():
            finalized.set()

        self.rag.ainsert = index
        self.rag.finalize_storages = finalize
        self.rag.role_llm_funcs = {}
        indexing = asyncio.create_task(
            self.runtime.index_text(
                source_id=self.revision_id,
                text="evidence",
                file_path="source.txt",
            )
        )
        await index_started.wait()
        closing = asyncio.create_task(self.runtime.close())
        await asyncio.sleep(0)
        self.assertFalse(finalized.is_set())
        release_index.set()
        await indexing
        await closing
        self.assertTrue(finalized.is_set())


if __name__ == "__main__":
    unittest.main()
