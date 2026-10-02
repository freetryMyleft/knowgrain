from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import UUID, uuid4

from knowgrain.config import Settings
from knowgrain.evidence_access import EvidenceAccess, EvidenceFileError
from knowgrain.source_archive_files import ArchiveEntry, SourceArchiveFiles
from knowgrain.source_repository import SourceConflictError, SourceNotFoundError
from knowgrain.source_service import SourceLifecycleUnavailableError, SourceService
from knowgrain.vault import VaultStore


SOURCE_ID = UUID("a4e6eb8f-4c32-4cb5-9545-c16e82c143a0")
LATEST_ID = UUID("66a0f9f9-95a2-4b21-b62d-a3e37ee1ad8c")
CURRENT_ID = UUID("b43e4ca6-44a9-41bd-bbc1-b0959f9a0999")


def source_snapshot(*, latest_id=LATEST_ID, current_id=CURRENT_ID) -> dict:
    def revision(revision_id: UUID, filename: str) -> dict:
        return {
            "id": str(revision_id),
            "filename": filename,
            "vault_path": f"Sources/{revision_id}.txt",
            "sha256": "a" * 64,
        }

    return {
        "id": str(SOURCE_ID),
        "state": "deleted",
        "lifecycle_version": 1,
        "latest_revision_id": str(latest_id) if latest_id else None,
        "current_revision_id": str(current_id) if current_id else None,
        "latest_revision": revision(latest_id, "latest.txt") if latest_id else None,
        "current_revision": revision(current_id, "current.txt") if current_id else None,
    }


class FakeRepository:
    def __init__(self, snapshot: dict | None = None):
        self.snapshot = snapshot or source_snapshot()
        self.calls: list[tuple[str, dict]] = []
        self.delete_result = {"state": "deleted", "lifecycle_version": 1}
        self.restore_result = {"state": "active", "lifecycle_version": 2}

    async def get_source(self, source_id):
        return self.snapshot if source_id == SOURCE_ID else None

    async def soft_delete_source(self, source_id, **kwargs):
        self.calls.append(("delete", {"source_id": source_id, **kwargs}))
        return self.delete_result

    async def restore_source(self, source_id, **kwargs):
        self.calls.append(("restore", {"source_id": source_id, **kwargs}))
        return self.restore_result


class FakeEvidenceAccess:
    def __init__(self):
        self.reads = []
        self.error: EvidenceFileError | None = None

    def original_revision(self, relative, expected_sha256, *, allow_archived=True):
        self.reads.append((relative, expected_sha256))
        if self.error:
            raise self.error
        return b"verified"


class SourceLifecycleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name) / "vault")
        self.repository = FakeRepository()
        self.evidence = FakeEvidenceAccess()
        self.service = SourceService(
            Settings(_env_file=None), self.repository, self.vault, self.evidence
        )

    async def test_soft_delete_forwards_compare_and_swap_fields(self):
        latest = uuid4()
        result = await self.service.soft_delete_source(
            SOURCE_ID,
            expected_lifecycle_version=7,
            expected_latest_revision_id=latest,
        )
        self.assertEqual(result, self.repository.delete_result)
        self.assertEqual(
            self.repository.calls,
            [("delete", {
                "source_id": SOURCE_ID,
                "expected_lifecycle_version": 7,
                "expected_latest_revision_id": latest,
            })],
        )

    async def test_restore_verifies_distinct_latest_and_current_then_cas(self):
        result = await self.service.restore_source(
            SOURCE_ID,
            expected_lifecycle_version=1,
            expected_latest_revision_id=LATEST_ID,
        )

        self.assertEqual(result, self.repository.restore_result)
        self.assertEqual(len(self.evidence.reads), 2)
        self.assertEqual(
            self.repository.calls,
            [("restore", {
                "source_id": SOURCE_ID,
                "expected_lifecycle_version": 1,
                "expected_latest_revision_id": LATEST_ID,
                "verified_current_revision_id": CURRENT_ID,
            })],
        )

    async def test_restore_reads_revision_once_when_current_equals_latest(self):
        self.repository.snapshot = source_snapshot(current_id=LATEST_ID)

        await self.service.restore_source(
            SOURCE_ID,
            expected_lifecycle_version=1,
            expected_latest_revision_id=LATEST_ID,
        )

        self.assertEqual(len(self.evidence.reads), 1)

    async def test_legacy_service_cannot_activate_originals_still_in_trash(self):
        self.vault.initialize()
        content = b"retained original"
        path = self.vault.write_source(SOURCE_ID, LATEST_ID, "original.txt", content)
        digest = hashlib.sha256(content).hexdigest()
        SourceArchiveFiles(self.vault).archive(SOURCE_ID, (
            ArchiveEntry(LATEST_ID, path, digest),
        ))
        self.repository.snapshot = source_snapshot(current_id=LATEST_ID)
        for field in ("latest_revision", "current_revision"):
            self.repository.snapshot[field].update(vault_path=path, sha256=digest)
        service = SourceService(Settings(_env_file=None), self.repository, self.vault)
        self.assertEqual(EvidenceAccess(self.vault).original_revision(path, digest), content)
        with self.assertRaises(SourceConflictError):
            await service.restore_source(
                SOURCE_ID, expected_lifecycle_version=1,
                expected_latest_revision_id=LATEST_ID,
            )
        self.assertEqual(self.repository.calls, [])

    async def test_restore_maps_missing_or_changed_files_to_safe_conflict(self):
        for code in ("missing", "conflict"):
            with self.subTest(code=code):
                self.evidence.error = EvidenceFileError(code)
                with self.assertRaises(SourceConflictError) as error:
                    await self.service.restore_source(
                        SOURCE_ID,
                        expected_lifecycle_version=1,
                        expected_latest_revision_id=LATEST_ID,
                    )
                self.assertIn("Vault 中的原件", str(error.exception))
                self.assertNotIn("Evidence file", str(error.exception))
                self.assertFalse(self.repository.calls)

    async def test_restore_maps_unsafe_file_access_to_unavailable(self):
        self.evidence.error = EvidenceFileError("unavailable")
        with self.assertRaises(SourceLifecycleUnavailableError):
            await self.service.restore_source(
                SOURCE_ID,
                expected_lifecycle_version=1,
                expected_latest_revision_id=LATEST_ID,
            )
        self.assertFalse(self.repository.calls)

    async def test_restore_of_unknown_source_returns_not_found(self):
        with self.assertRaises(SourceNotFoundError):
            await self.service.restore_source(
                uuid4(),
                expected_lifecycle_version=1,
                expected_latest_revision_id=None,
            )
        self.assertFalse(self.evidence.reads)

    async def test_cancellation_waits_for_vault_reader_thread_before_returning(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingReader:
            def original_revision(self, relative, expected_sha256, *, allow_archived=True):
                started.set()
                release.wait()
                return b"verified"

        self.service.evidence_access = BlockingReader()
        task = asyncio.create_task(self.service.restore_source(
            SOURCE_ID,
            expected_lifecycle_version=1,
            expected_latest_revision_id=LATEST_ID,
        ))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done(), "cancelled restore must join its Vault worker")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.repository.calls)


if __name__ == "__main__":
    unittest.main()
