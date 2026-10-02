from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import UUID, uuid4

from knowgrain.source_archive_files import ArchiveEntry, SourceArchiveFiles
from knowgrain.source_file_service import (
    SourceFileLeaseLostError,
    SourceFileService,
    SourceLifecycleUnavailableError,
)
from knowgrain.source_repository import SourceConflictError
from knowgrain.vault import VaultStore


SOURCE_ID = UUID("a4e6eb8f-4c32-4cb5-9545-c16e82c143a0")


class FileOperationRepositoryStub:
    def __init__(self):
        self.operations: dict[UUID, dict] = {}
        self.fail_completion = False
        self.completed = []
        self.events = []
        self.released_owner = None
        self.lock = None

    def add_operation(self, source_id, kind, entries):
        operation_id = uuid4()
        operation = {
            "operation_id": operation_id,
            "source_id": source_id,
            "lifecycle_version": 1,
            "kind": kind,
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
            "expected_latest_revision_id": entries[-1].revision_id,
            "verified_current_revision_id": entries[-1].revision_id,
        }
        self.operations[operation_id] = operation
        return operation

    async def prepare_restore(
        self, source_id, *, expected_lifecycle_version, expected_latest_revision_id
    ):
        if self.lock is not None:
            self.assert_lock_held = self.lock.locked()
        self.events.append("prepare")
        return next(
            operation
            for operation in self.operations.values()
            if operation["source_id"] == source_id and operation["kind"] == "restore"
        )

    async def claim_file_operation(self, owner, *, operation_id=None):
        choices = (
            [self.operations[operation_id]]
            if operation_id in self.operations
            else list(self.operations.values())
        )
        operation = next((item for item in choices if item["state"] == "queued"), None)
        if operation is None:
            return None
        operation["state"] = "running"
        operation["attempts"] += 1
        operation["lease_owner"] = owner
        self.events.append(("claim", operation["operation_id"]))
        return dict(operation)

    async def renew_file_lease(self, operation_id, owner):
        self.events.append(("renew", operation_id))
        operation = self.operations[operation_id]
        return operation["state"] == "running" and operation["lease_owner"] == owner

    async def complete_file_operation(self, operation_id, owner):
        operation = self.operations[operation_id]
        if self.fail_completion:
            self.fail_completion = False
            raise RuntimeError("database transaction failed")
        if operation["state"] != "running" or operation["lease_owner"] != owner:
            return None
        operation["state"] = "succeeded"
        operation["lease_owner"] = None
        self.events.append("complete")
        self.completed.append(operation_id)
        return {"state": "active", "lifecycle_version": 2}

    async def fail_file_operation(self, operation_id, owner, safe_error):
        operation = self.operations[operation_id]
        if operation["state"] != "running" or operation["lease_owner"] != owner:
            return False
        operation["state"] = "failed"
        operation["error"] = safe_error
        operation["lease_owner"] = None
        self.events.append("fail")
        return True

    async def release_file_owner(self, owner):
        self.released_owner = owner
        self.events.append("release")
        for operation in self.operations.values():
            if operation["state"] == "running" and operation["lease_owner"] == owner:
                operation["state"] = "queued"
                operation["lease_owner"] = None

    async def enqueue_cleaned_sources(self, *, limit=100):
        self.events.append(("scan", limit))
        return 0


class SourceFileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "vault"
        self.vault = VaultStore(self.root)
        self.vault.initialize()
        self.files = SourceArchiveFiles(self.vault)
        self.lock = asyncio.Lock()
        self.repository = FileOperationRepositoryStub()
        self.service = SourceFileService(self.repository, self.files, self.lock)
        self.repository.lock = self.lock
        self.entries = self._write_entries()

    def _write_entries(self):
        results = []
        revisions = (
            (".txt", b"first registered revision"),
            (".md", b"reviewed source notes"),
        )
        for suffix, content in revisions:
            revision_id = uuid4()
            vault_path = self.vault.write_source(
                SOURCE_ID, revision_id, f"source{suffix}", content
            )
            results.append(
                ArchiveEntry(revision_id, vault_path, hashlib.sha256(content).hexdigest())
            )
        return tuple(results)

    async def test_sync_restore_moves_complete_manifest_before_database_activation(self):
        self.files.archive(SOURCE_ID, self.entries)
        self.repository.add_operation(SOURCE_ID, "restore", self.entries)

        result = await self.service.restore_source(
            SOURCE_ID,
            expected_lifecycle_version=1,
            expected_latest_revision_id=self.entries[-1].revision_id,
        )

        self.assertEqual(result, {"state": "active", "lifecycle_version": 2})
        self.assertEqual(self.files.location(SOURCE_ID, self.entries), "vault")
        claim_index = next(
            index for index, event in enumerate(self.repository.events)
            if isinstance(event, tuple) and event[0] == "claim"
        )
        self.assertTrue(self.repository.assert_lock_held)
        self.assertLess(self.repository.events.index("prepare"), claim_index)
        self.assertLess(claim_index, self.repository.events.index("complete"))
        first_operation = next(iter(self.repository.operations.values()))
        self.assertEqual(first_operation["state"], "succeeded")
        self.assertIs(self.lock.locked(), False)

    async def test_archive_failure_maps_to_safe_conflict_and_is_journaled(self):
        self.repository.add_operation(SOURCE_ID, "archive", self.entries)
        stray = self.root / "Sources" / "Files" / str(SOURCE_ID) / "unregistered.txt"
        stray.write_text("keep me")

        await self.service.run_next()

        operation = next(iter(self.repository.operations.values()))
        self.assertEqual(operation["state"], "failed")
        self.assertNotIn("keep me", operation["error"])
        self.assertNotIn("complete", self.repository.events)
        self.assertTrue(stray.exists())

    async def test_database_completion_failure_keeps_moved_restore_replayable(self):
        self.files.archive(SOURCE_ID, self.entries)
        operation = self.repository.add_operation(SOURCE_ID, "restore", self.entries)
        self.repository.fail_completion = True

        with self.assertRaises(SourceLifecycleUnavailableError):
            await self.service.restore_source(
                SOURCE_ID,
                expected_lifecycle_version=1,
                expected_latest_revision_id=self.entries[-1].revision_id,
            )

        self.assertEqual(self.files.location(SOURCE_ID, self.entries), "vault")
        self.assertEqual(operation["state"], "queued")
        self.assertNotIn(operation["operation_id"], self.repository.completed)

        result = await self.service.restore_source(
            SOURCE_ID,
            expected_lifecycle_version=1,
            expected_latest_revision_id=self.entries[-1].revision_id,
        )
        self.assertEqual(result["state"], "active")
        self.assertEqual(operation["state"], "succeeded")

    async def test_lease_loss_drains_thread_and_replay_verifies_actual_location(self):
        operation = self.repository.add_operation(SOURCE_ID, "archive", self.entries)
        started = asyncio.Event()
        release_thread = threading.Event()
        loop = asyncio.get_running_loop()
        original_archive = self.files.archive

        def blocking_archive(source_id, entries):
            loop.call_soon_threadsafe(started.set)
            release_thread.wait()
            original_archive(source_id, entries)

        self.files.archive = blocking_archive

        async def lose_lease(operation_id):
            await started.wait()
            raise SourceFileLeaseLostError

        self.service._renew_while_running = lose_lease
        task = asyncio.create_task(self.service.run_next())
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.lock.locked())
        self.assertIsNone(self.repository.released_owner)

        release_thread.set()
        self.assertFalse(await asyncio.wait_for(task, timeout=2))
        self.assertEqual(operation["state"], "queued")
        self.assertTrue(self.root.joinpath("Trash", "Files", str(SOURCE_ID)).is_dir())

        self.files.archive = original_archive
        self.service._renew_while_running = (
            SourceFileService._renew_while_running.__get__(self.service)
        )
        self.assertTrue(await self.service.run_next())
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(self.repository.completed, [operation["operation_id"]])

    async def test_shared_lock_serializes_same_owner_release_before_next_claim(self):
        first = self.repository.add_operation(SOURCE_ID, "archive", self.entries)
        other_source = uuid4()
        other_revision = uuid4()
        other_bytes = b"second source"
        other_path = self.vault.write_source(
            other_source, other_revision, "other.txt", other_bytes
        )
        second_entries = (
            ArchiveEntry(other_revision, other_path, hashlib.sha256(other_bytes).hexdigest()),
        )
        second = self.repository.add_operation(other_source, "archive", second_entries)
        started = asyncio.Event()
        release_thread = threading.Event()
        loop = asyncio.get_running_loop()
        original_archive = self.files.archive

        def block_first(source_id, entries):
            if source_id == SOURCE_ID:
                if ("renew", first["operation_id"]) not in self.repository.events:
                    raise AssertionError("lease must be renewed immediately before file I/O")
                loop.call_soon_threadsafe(started.set)
                release_thread.wait()
            original_archive(source_id, entries)

        self.files.archive = block_first
        first_task = asyncio.create_task(self.service.run_next())
        await asyncio.wait_for(started.wait(), timeout=2)
        second_task = asyncio.create_task(self.service.run_next())
        await asyncio.sleep(0.02)
        claims = [
            event
            for event in self.repository.events
            if isinstance(event, tuple) and event[0] == "claim"
        ]
        self.assertEqual(claims, [("claim", first["operation_id"])])

        release_thread.set()
        self.assertTrue(await asyncio.wait_for(first_task, timeout=2))
        self.assertTrue(await asyncio.wait_for(second_task, timeout=2))
        self.assertEqual(first["state"], "succeeded")
        self.assertEqual(second["state"], "succeeded")
        claims = [
            (index, event[1])
            for index, event in enumerate(self.repository.events)
            if isinstance(event, tuple) and event[0] == "claim"
        ]
        releases = [
            index for index, event in enumerate(self.repository.events) if event == "release"
        ]
        self.assertLess(releases[0], claims[1][0])

    async def test_restore_manifest_conflict_does_not_activate_source(self):
        self.files.archive(SOURCE_ID, self.entries)
        operation = self.repository.add_operation(SOURCE_ID, "restore", self.entries)
        trash_path = (
            self.root
            / "Trash"
            / "Files"
            / str(SOURCE_ID)
            / Path(self.entries[0].vault_path).name
        )
        trash_path.write_bytes(b"changed after archive")

        with self.assertRaises(SourceConflictError):
            await self.service.restore_source(
                SOURCE_ID,
                expected_lifecycle_version=1,
                expected_latest_revision_id=self.entries[-1].revision_id,
            )

        self.assertEqual(operation["state"], "failed")
        self.assertNotIn(operation["operation_id"], self.repository.completed)
        self.assertTrue(trash_path.exists())


if __name__ == "__main__":
    unittest.main()
