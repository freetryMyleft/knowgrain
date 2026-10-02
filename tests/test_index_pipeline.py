import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import threading
from unittest.mock import patch
from uuid import uuid4

from knowgrain.job_runner import IndexJobRunner, JobLeaseLostError
from knowgrain.vault import VaultStore


class RepositoryStub:
    def __init__(self):
        self.failure = None
        self.completion = None

    async def fail_job(self, job_id, owner, error):
        self.failure = error
        return True

    async def complete_job(self, job_id, owner, text_sha256, segments):
        self.completion = {"text_sha256": text_sha256, "segments": segments}
        return True


class ModelStub:
    def __init__(self, failure=None, wait=False):
        self.failure = failure
        self.wait = wait
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def index_text(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.failure:
            raise self.failure
        if self.wait:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


class IndexPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name) / "vault")
        self.vault.initialize()
        self.source_id, self.revision_id = uuid4(), uuid4()
        self.repository = RepositoryStub()

    def job(self, content, filename="example.md"):
        path = self.vault.write_source(self.source_id, self.revision_id, filename, content)
        return {
            "job_id": str(uuid4()), "source_id": str(self.source_id),
            "revision_id": str(self.revision_id), "vault_path": path,
            "filename": filename, "sha256": hashlib.sha256(content).hexdigest(),
        }

    def runner(self, model):
        return IndexJobRunner(None, self.repository, self.vault, model)

    async def test_model_failure_keeps_original_and_marks_failed(self):
        original = "# 本地资料\n\n可核实的原文。".encode()
        job = self.job(original)
        await self.runner(ModelStub(failure=ConnectionError("private credential")))._execute(job)
        self.assertEqual(self.vault.read_bytes(job["vault_path"]), original)
        self.assertIn("ConnectionError", self.repository.failure)
        self.assertNotIn("private credential", self.repository.failure)
        self.assertIsNone(self.repository.completion)

    async def test_unparseable_source_keeps_original_and_marks_failed(self):
        job = self.job(b"\xff\xfe", "example.txt")
        model = ModelStub()
        await self.runner(model)._execute(job)
        self.assertEqual(self.vault.read_bytes(job["vault_path"]), b"\xff\xfe")
        self.assertIn("UTF-8", self.repository.failure)
        self.assertFalse(model.calls)

    async def test_changed_hash_cannot_be_indexed(self):
        job = self.job(b"original")
        job["sha256"] = "0" * 64
        model = ModelStub()
        await self.runner(model)._execute(job)
        self.assertIn("哈希", self.repository.failure)
        self.assertFalse(model.calls)

    async def test_completion_records_text_hash_and_revision_identity(self):
        job = self.job(b"# Heading\n\nEvidence text")
        model = ModelStub()
        await self.runner(model)._execute(job)
        self.assertEqual(model.calls[0]["source_id"], job["revision_id"])
        self.assertEqual(model.calls[0]["file_path"], job["vault_path"])
        self.assertEqual(
            self.repository.completion["text_sha256"],
            hashlib.sha256(model.calls[0]["text"].encode()).hexdigest(),
        )
        self.assertIsNone(self.repository.failure)

    async def test_lease_loss_cancels_inflight_index_without_committing(self):
        job = self.job(b"evidence")
        model = ModelStub(wait=True)
        runner = self.runner(model)

        async def lose_lease(job_id):
            await model.started.wait()
            raise JobLeaseLostError("lost")

        runner._renew = lose_lease
        await runner._execute(job)
        self.assertTrue(model.cancelled)
        self.assertIsNone(self.repository.completion)
        self.assertIsNone(self.repository.failure)

    async def test_shutdown_cancellation_awaits_inflight_index(self):
        job = self.job(b"evidence")
        model = ModelStub(wait=True)
        task = asyncio.create_task(self.runner(model)._execute(job))
        await model.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(model.cancelled)
        self.assertIsNone(self.repository.completion)

    async def test_cancellation_joins_file_worker_before_shutdown_returns(self):
        job = self.job(b"evidence")
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def delayed_read(*args):
            started.set()
            release.wait(5)
            finished.set()
            return b"evidence"

        model = ModelStub()
        with patch("knowgrain.job_runner.EvidenceAccess.original_revision", side_effect=delayed_read):
            task = asyncio.create_task(self.runner(model)._execute(job))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                task.cancel()  # Repeated shutdown signals must not detach the worker.
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertFalse(model.calls)
        self.assertIsNone(self.repository.completion)

    async def test_symlink_original_is_rejected_before_model_call(self):
        job = self.job(b"evidence")
        original = self.vault.resolve(job["vault_path"])
        replacement = original.with_name("outside.txt")
        replacement.write_bytes(b"evidence")
        original.unlink()
        original.symlink_to(replacement)
        model = ModelStub()
        await self.runner(model)._execute(job)
        self.assertFalse(model.calls)
        self.assertIn("安全读取", self.repository.failure)
