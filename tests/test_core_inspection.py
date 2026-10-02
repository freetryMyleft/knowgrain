import asyncio
import hashlib
import unittest
from types import SimpleNamespace
from uuid import uuid4

from knowgrain.config import Settings
from knowgrain.lightrag_runtime import LightRAGRuntime


class StrictStore:
    """Small stand-in for the strict point and bulk APIs used by PGKVStorage."""

    def __init__(self, records=None):
        self.records = dict(records or {})
        self.point_reads = []
        self.bulk_reads = []
        self.read_error = None

    async def get_by_id_strict(self, key):
        self.point_reads.append(key)
        if self.read_error is not None:
            raise self.read_error
        return self.records.get(key)

    async def get_by_ids(self, keys):
        self.bulk_reads.append(tuple(keys))
        if self.read_error is not None:
            raise self.read_error
        return [self.records.get(key) for key in keys]


class CoreInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.revision_id = str(uuid4())
        self.content = "source text"
        self.chunk_ids = ("chunk-0", "chunk-1")
        self.stores = {
            # PGDocStatusStorage's point projection deliberately omits id.
            "doc_status": StrictStore({
                self.revision_id: {
                    "status": "processed",
                    "chunks_list": list(self.chunk_ids),
                    "chunks_count": len(self.chunk_ids),
                },
            }),
            "full_docs": StrictStore({
                self.revision_id: {
                    "id": self.revision_id,
                    "content": self.content,
                },
            }),
            "full_entities": StrictStore({
                # These are the Core 1.5.7 empty recovery-anchor shapes.
                self.revision_id: {
                    "id": self.revision_id,
                    "entity_names": [],
                    "count": 0,
                },
            }),
            "full_relations": StrictStore({
                self.revision_id: {
                    "id": self.revision_id,
                    "relation_pairs": [],
                    "count": 0,
                },
            }),
            "text_chunks": StrictStore({
                chunk_id: {
                    "id": chunk_id,
                    "content": f"chunk content {index}",
                    "full_doc_id": self.revision_id,
                    "chunk_order_index": index,
                }
                for index, chunk_id in enumerate(self.chunk_ids)
            }),
        }
        self.rag = SimpleNamespace(**self.stores)
        self.runtime = LightRAGRuntime(Settings(_env_file=None))
        self.runtime._rag = self.rag

    def expected_hash(self):
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    async def inspect(self, **kwargs):
        kwargs.setdefault("source_id", self.revision_id)
        kwargs.setdefault("expected_text_sha256", self.expected_hash())
        return await self.runtime.inspect_revision(**kwargs)

    async def test_healthy_revision_uses_core_empty_anchor_shapes(self):
        result = await self.inspect(expected_chunk_ids=self.chunk_ids)

        self.assertEqual(result, {
            "state": "healthy",
            "chunk_ids": self.chunk_ids,
            "reason": None,
        })
        for name in ("doc_status", "full_docs", "full_entities", "full_relations"):
            self.assertEqual(self.stores[name].point_reads, [self.revision_id])

    async def test_fully_absent_revision_requires_all_known_chunks_to_be_absent(self):
        for name in ("doc_status", "full_docs", "full_entities", "full_relations"):
            self.stores[name].records.clear()
        self.stores["text_chunks"].records.clear()

        result = await self.inspect(expected_chunk_ids=("previous-chunk",))

        self.assertEqual(result, {
            "state": "missing",
            "chunk_ids": (),
            "reason": "revision_missing",
        })
        self.assertEqual(self.stores["text_chunks"].bulk_reads, [("previous-chunk",)])

    async def test_partial_status_is_inconsistent(self):
        self.stores["full_docs"].records.clear()

        result = await self.inspect()

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "document_record_missing")

    async def test_orphan_chunk_without_status_is_not_missing(self):
        for name in ("doc_status", "full_docs", "full_entities", "full_relations"):
            self.stores[name].records.clear()
        orphan_id = "orphan-chunk"
        self.stores["text_chunks"].records[orphan_id] = {
            "id": orphan_id,
            "content": "orphan content",
            "full_doc_id": self.revision_id,
            "chunk_order_index": 0,
        }

        result = await self.inspect(expected_chunk_ids=(orphan_id,))

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "orphan_chunks_without_status")

    async def test_full_document_content_hash_mismatch_is_inconsistent(self):
        result = await self.inspect(expected_text_sha256="0" * 64)

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "text_hash_mismatch")

    async def test_foreign_chunk_is_inconsistent(self):
        self.stores["text_chunks"].records[self.chunk_ids[0]]["full_doc_id"] = str(uuid4())

        result = await self.inspect()

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "chunk_owner_mismatch")

    async def test_mismatched_stored_chunk_id_is_inconsistent(self):
        self.stores["text_chunks"].records[self.chunk_ids[0]]["id"] = "different-id"

        result = await self.inspect()

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "chunk_record_invalid")

    async def test_unique_noncontiguous_chunk_order_is_accepted(self):
        self.stores["text_chunks"].records[self.chunk_ids[0]]["chunk_order_index"] = 4
        self.stores["text_chunks"].records[self.chunk_ids[1]]["chunk_order_index"] = 9

        result = await self.inspect()

        self.assertEqual(result["state"], "healthy")

    async def test_duplicate_chunk_order_is_inconsistent(self):
        self.stores["text_chunks"].records[self.chunk_ids[1]]["chunk_order_index"] = 0

        result = await self.inspect()

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "chunk_record_invalid")

    async def test_positional_chunk_id_must_match_its_order(self):
        old_id = self.chunk_ids[0]
        positional_id = f"{self.revision_id}-chunk-007"
        first_chunk = self.stores["text_chunks"].records.pop(old_id)
        first_chunk["id"] = positional_id
        self.stores["text_chunks"].records[positional_id] = first_chunk
        self.stores["doc_status"].records[self.revision_id]["chunks_list"][0] = positional_id

        result = await self.inspect()

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "chunk_record_invalid")

    async def test_processed_nonempty_document_with_no_chunks_is_inconsistent(self):
        self.stores["doc_status"].records[self.revision_id]["chunks_list"] = []
        self.stores["doc_status"].records[self.revision_id]["chunks_count"] = 0
        self.stores["text_chunks"].records.clear()

        result = await self.inspect()

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "chunk_manifest_empty")

    async def test_expected_chunk_manifest_must_match_core_manifest(self):
        result = await self.inspect(expected_chunk_ids=("previous-core-chunk",))

        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["reason"], "chunk_manifest_mismatch")

    async def test_strict_read_failure_propagates_as_safe_runtime_error(self):
        self.stores["full_docs"].read_error = RuntimeError("private database detail")

        with self.assertRaisesRegex(RuntimeError, "inspection read failed") as raised:
            await self.inspect()

        self.assertNotIn("private database detail", str(raised.exception))

    async def test_cancel_while_waiting_for_write_lock_does_not_release_owner_lock(self):
        await self.runtime._write_lock.acquire()
        inspection = asyncio.create_task(self.inspect())
        await asyncio.sleep(0)
        inspection.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await inspection

        self.assertTrue(self.runtime._write_lock.locked())
        for store in self.stores.values():
            self.assertEqual(store.point_reads, [])
        self.runtime._write_lock.release()


if __name__ == "__main__":
    unittest.main()
