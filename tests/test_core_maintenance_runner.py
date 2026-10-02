import asyncio
import unittest
from uuid import uuid4

from knowgrain.core_maintenance_runner import (
    CoreMaintenanceRunner,
    MaintenanceLeaseLostError,
)


class DatabaseStub:
    is_ready = True


class MaintenanceRepositoryStub:
    def __init__(self, *, record_result=True, renew_result=True, complete_result=True):
        self.job_id = uuid4()
        self.revision_id = uuid4()
        self.cleanup_chunk_ids = ["prior-chunk"]
        self.record_result = record_result
        self.renew_result = renew_result
        self.complete_result = complete_result
        self.failure = None
        self.completed = None
        self.manifest = None
        self.events = []
        self.record_started = asyncio.Event()
        self.record_release = asyncio.Event()
        self.wait_in_record = False
        self.released_owner = None

    def job(self):
        return {
            "job_id": str(self.job_id),
            "source_id": str(uuid4()),
            "revision_id": str(self.revision_id),
            "lifecycle_version": 2,
            "cleanup_chunk_ids": self.cleanup_chunk_ids,
        }

    async def claim_maintenance(self, owner):
        self.claimed_owner = owner
        return self.job()

    async def renew_maintenance_lease(self, job_id, owner):
        return self.renew_result

    async def record_maintenance_chunks(self, job_id, owner, chunk_ids):
        self.record_started.set()
        if self.wait_in_record:
            await self.record_release.wait()
        self.manifest = tuple(chunk_ids)
        self.events.append(("manifest", self.manifest))
        return self.record_result

    async def complete_maintenance(self, job_id, owner):
        self.completed = (job_id, owner)
        return self.complete_result

    async def fail_maintenance(self, job_id, owner, safe_error):
        self.failure = safe_error
        return True

    async def release_maintenance_owner(self, owner):
        self.released_owner = owner


class CoreStub:
    is_ready = True
    restart_required = False
    model_validation_error = None

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait_after_manifest = False
        self.mutated = False
        self.result = None
        self.failure = None
        self.calls = []

    async def delete_revision(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await kwargs["persist_manifest"](("current-chunk", "other-current-chunk"))
        if self.wait_after_manifest:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        self.mutated = True
        return self.result


class CoreMaintenanceRunnerTests(unittest.IsolatedAsyncioTestCase):
    def make_runner(self, repository=None, core=None):
        repository = repository or MaintenanceRepositoryStub()
        core = core or CoreStub()
        return CoreMaintenanceRunner(DatabaseStub(), repository, core), repository, core

    async def test_persists_manifest_before_core_delete_and_completes_strictly(self):
        runner, repository, core = self.make_runner()

        await runner._execute(repository.job())

        self.assertEqual(
            repository.events,
            [("manifest", ("current-chunk", "other-current-chunk"))],
        )
        self.assertTrue(core.mutated)
        self.assertEqual(core.calls[0]["source_id"], str(repository.revision_id))
        self.assertEqual(core.calls[0]["expected_chunk_ids"], ["prior-chunk"])
        self.assertIs(core.calls[0]["delete_llm_cache"], False)
        self.assertTrue(callable(core.calls[0]["persist_manifest"]))
        self.assertIsNotNone(repository.completed)
        self.assertIsNone(repository.failure)

    async def test_manifest_expired_lease_prevents_delete_and_completion(self):
        repository = MaintenanceRepositoryStub(record_result=False)
        runner, _, core = self.make_runner(repository=repository)

        await runner._execute(repository.job())

        self.assertFalse(core.mutated)
        self.assertIsNone(repository.completed)
        self.assertIsNone(repository.failure)

    async def test_completion_requires_exact_true(self):
        repository = MaintenanceRepositoryStub(complete_result=1)
        runner, _, core = self.make_runner(repository=repository)

        with self.assertRaises(MaintenanceLeaseLostError):
            await runner._delete_revision(repository.job())

        self.assertTrue(core.mutated)
        self.assertIsNotNone(repository.completed)
        self.assertIsNone(repository.failure)

    async def test_core_error_is_sanitized_before_persistence(self):
        repository = MaintenanceRepositoryStub()
        core = CoreStub()
        core.failure = ValueError("private user document content")
        runner, _, _ = self.make_runner(repository=repository, core=core)

        await runner._execute(repository.job())

        self.assertEqual(
            repository.failure,
            "Core 清理失败 (ValueError)；检查存储后重试",
        )
        self.assertNotIn("private user document content", repository.failure)

    async def test_cancellation_joins_cleanup_and_keeps_lease_until_call_finishes(self):
        repository = MaintenanceRepositoryStub()
        core = CoreStub()
        core.wait_after_manifest = True
        runner, _, _ = self.make_runner(repository=repository, core=core)
        task = asyncio.create_task(runner._execute(repository.job()))
        await core.started.wait()

        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        self.assertFalse(core.release.is_set())

        core.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(core.mutated)
        self.assertIsNone(repository.completed)

    async def test_shutdown_waits_for_manifest_callback_lease_check(self):
        repository = MaintenanceRepositoryStub()
        repository.wait_in_record = True
        core = CoreStub()
        runner, _, _ = self.make_runner(repository=repository, core=core)
        runner.start()
        await repository.record_started.wait()

        stopping = asyncio.create_task(runner.stop())
        await asyncio.sleep(0.02)
        self.assertFalse(stopping.done())
        self.assertFalse(core.mutated)
        self.assertIsNone(repository.released_owner)

        repository.record_release.set()
        await stopping

        self.assertTrue(core.mutated)
        self.assertIsNone(repository.completed)
        self.assertEqual(repository.released_owner, runner.owner)

    async def test_lease_loss_joins_cleanup_without_acknowledging_it(self):
        repository = MaintenanceRepositoryStub()
        core = CoreStub()
        core.wait_after_manifest = True
        runner, _, _ = self.make_runner(repository=repository, core=core)

        async def lose_lease(job_id):
            await core.started.wait()
            raise MaintenanceLeaseLostError("expired")

        runner._renew = lose_lease
        task = asyncio.create_task(runner._execute(repository.job()))
        await core.started.wait()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())

        core.release.set()
        await task

        self.assertTrue(core.mutated)
        self.assertIsNone(repository.completed)
        self.assertIsNone(repository.failure)
