from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from knowgrain.source_archive_files import ArchiveEntry, SourceArchiveFiles
from knowgrain.source_file_runner import SourceFileRunner
from knowgrain.source_file_service import SourceFileService
from knowgrain.vault import VaultStore


class DatabaseStub:
    is_ready = True


class RunnerRepositoryStub:
    def __init__(self, files, source_id, entries):
        self.files = files
        self.source_id = source_id
        self.entries = entries
        self.operation = {
            "operation_id": uuid4(),
            "source_id": source_id,
            "lifecycle_version": 1,
            "kind": "archive",
            "state": "queued",
            "attempts": 0,
            "manifest": [
                {
                    "revision_id": entry.revision_id,
                    "vault_path": entry.vault_path,
                    "sha256": entry.sha256,
                }
                for entry in entries
            ],
        }
        self.scan_calls = []
        self.completed = asyncio.Event()
        self.owner = None

    async def enqueue_cleaned_sources(self, *, limit=100):
        self.scan_calls.append(limit)
        return 0

    async def claim_file_operation(self, owner, *, operation_id=None):
        if self.operation["state"] != "queued":
            return None
        if operation_id is not None and operation_id != self.operation["operation_id"]:
            return None
        self.owner = owner
        self.operation["state"] = "running"
        return dict(self.operation)

    async def renew_file_lease(self, operation_id, owner):
        return self.operation["state"] == "running" and owner == self.owner

    async def complete_file_operation(self, operation_id, owner):
        if self.operation["state"] != "running" or owner != self.owner:
            return None
        self.operation["state"] = "succeeded"
        self.completed.set()
        return {"state": "succeeded"}

    async def fail_file_operation(self, operation_id, owner, safe_error):
        self.operation["state"] = "failed"
        self.operation["error"] = safe_error
        return True

    async def release_file_owner(self, owner):
        if owner != self.owner:
            return
        if self.operation["state"] == "running":
            self.operation["state"] = "queued"
        self.owner = None


class SourceFileRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name) / "vault"
        self.vault = VaultStore(root)
        self.vault.initialize()
        self.files = SourceArchiveFiles(self.vault)
        self.source_id = uuid4()
        revision_id = uuid4()
        path = self.vault.write_source(self.source_id, revision_id, "source.txt", b"runner source")
        self.entries = (
            ArchiveEntry(revision_id, path, hashlib.sha256(b"runner source").hexdigest()),
        )
        self.database = DatabaseStub()
        self.repository = RunnerRepositoryStub(self.files, self.source_id, self.entries)
        self.service = SourceFileService(self.repository, self.files, asyncio.Lock())

    async def test_runner_scans_and_archives_when_database_and_vault_are_ready(self):
        ready = False
        runner = SourceFileRunner(
            self.database,
            self.repository,
            self.service,
            vault_ready=lambda: ready,
        )
        with patch("knowgrain.source_file_runner._POLL_SECONDS", 0.01), patch(
            "knowgrain.source_file_runner._SCAN_INTERVAL_SECONDS", 10
        ):
            runner.start()
            await asyncio.sleep(0.03)
            self.assertEqual(self.repository.scan_calls, [])
            self.assertEqual(self.repository.operation["state"], "queued")

            ready = True
            await asyncio.wait_for(self.repository.completed.wait(), timeout=2)
            self.assertEqual(self.repository.scan_calls[0], 100)
            self.assertEqual(self.repository.operation["state"], "succeeded")
            self.assertEqual(self.files.location(self.source_id, self.entries), "trash")
            await runner.stop()

    async def test_stop_waits_for_cancelled_file_thread_before_owner_release(self):
        started = asyncio.Event()
        release_thread = threading.Event()
        loop = asyncio.get_running_loop()
        original_archive = self.files.archive

        def blocking_archive(source_id, entries):
            loop.call_soon_threadsafe(started.set)
            release_thread.wait()
            original_archive(source_id, entries)

        self.files.archive = blocking_archive
        runner = SourceFileRunner(
            self.database,
            self.repository,
            self.service,
            vault_ready=lambda: True,
        )
        with patch("knowgrain.source_file_runner._POLL_SECONDS", 0.01):
            runner.start()
            await asyncio.wait_for(started.wait(), timeout=2)
            stopping = asyncio.create_task(runner.stop())
            await asyncio.sleep(0.03)
            self.assertFalse(stopping.done())
            self.assertIsNotNone(self.repository.owner)

            release_thread.set()
            await asyncio.wait_for(stopping, timeout=2)

        self.assertEqual(self.repository.operation["state"], "queued")
        self.assertIsNone(self.repository.owner)
        self.assertEqual(self.files.location(self.source_id, self.entries), "trash")


if __name__ == "__main__":
    unittest.main()
