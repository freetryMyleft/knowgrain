import asyncio
from contextlib import suppress
import logging
from uuid import UUID, uuid4

from knowgrain.database import ApplicationDatabase
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.source_repository import SourceRepository

logger = logging.getLogger(__name__)


class MaintenanceLeaseLostError(RuntimeError):
    """The maintenance job is no longer owned by this executor."""


class CoreMaintenanceRunner:
    """Run Core revision cleanup on the event loop that owns LightRAG."""

    def __init__(
        self,
        database: ApplicationDatabase,
        repository: SourceRepository,
        lightrag: LightRAGRuntime,
    ) -> None:
        self.database = database
        self.repository = repository
        self.lightrag = lightrag
        self.owner = uuid4()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="knowgrain-core-maintenance-jobs"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.database.is_ready:
            try:
                await self.repository.release_maintenance_owner(self.owner)
            except Exception as exc:
                logger.warning(
                    "Core maintenance lease release failed (%s); leases will expire",
                    type(exc).__name__,
                )

    async def _run(self) -> None:
        while True:
            if (
                not self.database.is_ready
                or not self.lightrag.is_ready
                or self.lightrag.restart_required
                or self.lightrag.model_validation_error is not None
            ):
                await asyncio.sleep(1)
                continue
            try:
                job = await self.repository.claim_maintenance(self.owner)
                if job is not None:
                    await self._execute(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Core maintenance executor unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)

    async def _execute(self, job: dict) -> None:
        job_id = UUID(str(job["job_id"]))
        processing = asyncio.create_task(
            self._delete_revision(job), name=f"core-maintenance-{job_id}"
        )
        lease = asyncio.create_task(
            self._renew(job_id), name=f"core-maintenance-lease-{job_id}"
        )
        try:
            done, _ = await asyncio.wait(
                {processing, lease}, return_when=asyncio.FIRST_COMPLETED
            )
            if lease in done:
                try:
                    await lease
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Once renewal is uncertain, this worker cannot safely commit a result.
                    logger.warning(
                        "Core maintenance job %s lost lease (%s)",
                        job_id,
                        type(exc).__name__,
                    )
                    return
                raise RuntimeError("Maintenance lease renewal stopped unexpectedly")
            await processing
        except asyncio.CancelledError:
            raise
        except MaintenanceLeaseLostError:
            logger.warning("Core maintenance job %s lost its lease", job_id)
        except Exception as exc:
            safe_error = f"Core 清理失败 ({type(exc).__name__})；检查存储后重试"
            try:
                failed = await self.repository.fail_maintenance(
                    job_id, self.owner, safe_error
                )
                if failed is not True:
                    logger.warning("Core maintenance job %s failure was not recorded", job_id)
            except Exception as record_error:
                logger.warning(
                    "Core maintenance failure could not be recorded (%s)",
                    type(record_error).__name__,
                )
        finally:
            # Cleanup may already have crossed its durable manifest callback. Keep the
            # renewal task alive until that Core call has fully stopped before releasing
            # this owner or allowing application shutdown to finalize LightRAG.
            processing.cancel()
            cancelled_during_drain = await self._join_task(processing)
            lease.cancel()
            cancelled_during_drain |= await self._join_task(lease)
            if cancelled_during_drain:
                raise asyncio.CancelledError

    async def _delete_revision(self, job: dict) -> None:
        job_id = UUID(str(job["job_id"]))

        async def persist_manifest(chunk_ids: tuple[str, ...]) -> None:
            recorded = await self.repository.record_maintenance_chunks(
                job_id, self.owner, chunk_ids
            )
            if recorded is not True:
                raise MaintenanceLeaseLostError("Maintenance lease was lost")
            renewed = await self.repository.renew_maintenance_lease(job_id, self.owner)
            if renewed is not True:
                raise MaintenanceLeaseLostError("Maintenance lease was lost")

        result = await self._core_call_drained(
            self.lightrag.delete_revision,
            source_id=str(job["revision_id"]),
            expected_chunk_ids=job.get("cleanup_chunk_ids"),
            persist_manifest=persist_manifest,
            delete_llm_cache=False,
        )
        if result is not None:
            raise RuntimeError("LightRAG cleanup returned an invalid result")

        completed = await self.repository.complete_maintenance(job_id, self.owner)
        if completed is not True:
            raise MaintenanceLeaseLostError("Maintenance lease was lost before completion")

    async def _renew(self, job_id: UUID) -> None:
        while True:
            await asyncio.sleep(20)
            renewed = await self.repository.renew_maintenance_lease(job_id, self.owner)
            if renewed is not True:
                raise MaintenanceLeaseLostError("Maintenance lease was lost")

    @staticmethod
    async def _core_call_drained(function, **kwargs):
        """Do not detach a Core mutation when the worker is cancelled."""
        task = asyncio.create_task(function(**kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                if not task.done():
                    continue
                with suppress(BaseException):
                    task.result()
                raise
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError
                raise
            else:
                if cancelled:
                    raise asyncio.CancelledError
                return result

    @staticmethod
    async def _join_task(task: asyncio.Task) -> bool:
        """Join a child despite repeated cancellation; return whether cancellation arrived."""
        joined = asyncio.gather(task, return_exceptions=True)
        cancelled = False
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                cancelled = True
        return cancelled
