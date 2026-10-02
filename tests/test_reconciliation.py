"""Real-file checks for startup inspection and durable repair dispatch."""

import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from knowgrain.reconciliation import ReconciliationService
from knowgrain.vault import VaultStore


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name))
        self.vault.initialize()
        self.source, self.revision = uuid4(), uuid4()
        original = b"Repair the index while keeping this exact source revision."
        path = self.vault.write_source(self.source, self.revision, "source.txt", original)
        self.candidate = {
            "source_id": str(self.source), "revision_id": str(self.revision),
            "vault_path": path, "sha256": hashlib.sha256(original).hexdigest(),
            "parsed_text_sha256": hashlib.sha256(original).hexdigest(),
            "cleanup_chunk_ids": [],
        }
        self.repository = AsyncMock()
        self.repository.list_reconciliation_candidates.side_effect = [[self.candidate], []]
        self.repository.queue_reconciliation_repair.return_value = uuid4()
        self.core = AsyncMock()
        self.core.inspect_revision.return_value = {"state": "healthy", "chunk_ids": (), "reason": None}
        self.service = ReconciliationService(self.repository, self.core, self.vault, asyncio.Lock())

    async def test_healthy_source_is_not_requeued_or_written(self):
        result = await self.service.run()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["healthy"], 1)
        self.assertEqual(result["repair_queued"], 0)
        self.repository.queue_reconciliation_repair.assert_not_awaited()
        self.assertEqual(self.vault.read_bytes(self.candidate["vault_path"]),
                         b"Repair the index while keeping this exact source revision.")
        self.assertEqual(self.repository.list_reconciliation_candidates.call_args.kwargs["after_source_id"], self.source)

    async def test_missing_and_inconsistent_dispatch_to_existing_ledger(self):
        for state in ("missing", "inconsistent"):
            with self.subTest(state=state):
                self.repository.reset_mock()
                self.repository.list_reconciliation_candidates.side_effect = [[self.candidate], []]
                manifest = ("chunk-" + "a" * 32,)
                self.core.inspect_revision.return_value = {"state": state, "chunk_ids": manifest, "reason": state}
                result = await self.service.run()
                self.assertEqual(result["repair_queued"], 1)
                self.repository.queue_reconciliation_repair.assert_awaited_once_with(
                    self.candidate, chunk_ids=manifest, reason=state,
                )

    async def test_storage_failure_is_unknown_and_never_dispatches_missing(self):
        self.core.inspect_revision.side_effect = RuntimeError("private provider details")
        with self.assertRaises(RuntimeError):
            await self.service.run()
        self.assertEqual(self.service.report["state"], "unavailable")
        self.assertNotIn("private", self.service.report["detail"])
        self.repository.queue_reconciliation_repair.assert_not_awaited()

    async def test_changed_original_blocks_core_and_repair(self):
        (self.vault.root / self.candidate["vault_path"]).write_bytes(b"changed outside Web")
        with self.assertRaises(Exception):
            await self.service.run()
        self.assertEqual(self.service.report["state"], "unavailable")
        self.core.inspect_revision.assert_not_awaited()
        self.repository.queue_reconciliation_repair.assert_not_awaited()

    async def test_stale_cas_is_skipped_and_invalid_metadata_does_not_call_core(self):
        self.candidate["parsed_text_sha256"] = None
        self.repository.queue_reconciliation_repair.return_value = None
        result = await self.service.run()
        self.assertEqual(result["skipped"], 1)
        self.core.inspect_revision.assert_not_awaited()
        self.repository.queue_reconciliation_repair.assert_awaited_once_with(
            self.candidate, chunk_ids=(), reason="invalid_metadata",
        )

    async def test_cancellation_releases_lock_and_marks_unavailable(self):
        entered = asyncio.Event()
        async def inspect(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.core.inspect_revision.side_effect = inspect
        task = asyncio.create_task(self.service.run())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.file_lock.locked())
        self.assertEqual(self.service.report["state"], "unavailable")
        self.repository.queue_reconciliation_repair.assert_not_awaited()
