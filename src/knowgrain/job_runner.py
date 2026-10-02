import asyncio
from contextlib import suppress
from dataclasses import asdict
import hashlib
import logging
from uuid import UUID, uuid4

from knowgrain.database import ApplicationDatabase
from knowgrain.evidence_access import EvidenceAccess, EvidenceFileError
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.parsers import DocumentParseError, parse_document
from knowgrain.source_repository import SourceRepository
from knowgrain.vault import VaultStore

logger = logging.getLogger(__name__)


class SourceChangedError(ValueError):
    pass


class JobLeaseLostError(RuntimeError):
    pass


class IndexJobRunner:
    """Consume PostgreSQL jobs in the event loop that owns LightRAG."""

    def __init__(
        self,
        database: ApplicationDatabase,
        repository: SourceRepository,
        vault: VaultStore,
        lightrag: LightRAGRuntime,
    ) -> None:
        self.database = database
        self.repository = repository
        self.vault = vault
        self.lightrag = lightrag
        self.owner = uuid4()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="knowgrain-index-jobs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.database.is_ready:
            try:
                await self.repository.release_owner(self.owner)
            except Exception as exc:
                logger.warning("Job lease release failed (%s); leases will expire", type(exc).__name__)

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
                job = await self.repository.claim_job(self.owner)
                if job is not None:
                    await self._execute(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Index executor unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)

    async def _execute(self, job: dict) -> None:
        job_id = UUID(str(job["job_id"]))
        processing = asyncio.create_task(self._index(job), name=f"index-{job_id}")
        lease = asyncio.create_task(self._renew(job_id), name=f"lease-{job_id}")
        try:
            done, _ = await asyncio.wait({processing, lease}, return_when=asyncio.FIRST_COMPLETED)
            if lease in done:
                await lease
                raise JobLeaseLostError("Job lease was lost")
            await processing
        except asyncio.CancelledError:
            raise
        except JobLeaseLostError:
            logger.warning("Index job %s lost its lease", job_id)
        except Exception as exc:
            if isinstance(exc, (DocumentParseError, SourceChangedError)):
                detail = str(exc)
            else:
                detail = f"索引失败 ({type(exc).__name__})；检查数据库和模型后重试"
            try:
                await self.repository.fail_job(job_id, self.owner, detail)
            except Exception as record_error:
                logger.warning("Job failure could not be recorded (%s)", type(record_error).__name__)
        finally:
            processing.cancel()
            lease.cancel()
            # Await both before LightRAG can be finalized by application shutdown.
            await asyncio.gather(processing, lease, return_exceptions=True)

    async def _renew(self, job_id: UUID) -> None:
        while True:
            await asyncio.sleep(20)
            if not await self.repository.renew_lease(job_id, self.owner):
                raise JobLeaseLostError("Job lease was lost")

    async def _index(self, job: dict) -> None:
        try:
            content = await self._thread_drained(
                EvidenceAccess(self.vault).original_revision, job["vault_path"], job["sha256"]
            )
        except EvidenceFileError:
            raise SourceChangedError("Vault 原件缺失、无法安全读取或与修订哈希不一致；请检查原件后重试") from None
        parsed = await self._thread_drained(parse_document, job["filename"], content)
        await self.lightrag.index_text(
            source_id=str(job["revision_id"]), text=parsed.text, file_path=job["vault_path"]
        )
        completed = await self.repository.complete_job(
            UUID(str(job["job_id"])),
            self.owner,
            hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
            [asdict(segment) for segment in parsed.segments],
        )
        if not completed:
            raise JobLeaseLostError("Job lease was lost before completion")

    @staticmethod
    async def _thread_drained(function, *args):
        """Keep file/parse workers owned until completion, including cancellation."""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                if not task.done():
                    continue
                with suppress(Exception, asyncio.CancelledError):
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
