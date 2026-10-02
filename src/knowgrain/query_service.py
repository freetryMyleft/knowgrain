"""Durable evidence-first question answering on the owning asyncio loop."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import logging
from typing import Sequence
from uuid import UUID, uuid4

from knowgrain.config import Settings
from knowgrain.evidence_access import EvidenceAccess, EvidenceFileError
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.m3_types import Evidence, EvidenceUnavailableError
from knowgrain.provenance import ProvenanceService
from knowgrain.query_repository import QueryConflictError, QueryRepository
from knowgrain.query_contract import (
    INSUFFICIENT_MESSAGE,
    QueryValidationError,
    build_query_prompt,
    parse_answer,
    validate_question,
)


logger = logging.getLogger(__name__)
_RETRIEVAL_TIMEOUT_SECONDS = 240
_LEASE_SECONDS = 90
_LEASE_RENEW_SECONDS = 20


class QueryLeaseLostError(RuntimeError):
    """The worker no longer owns the durable query job lease."""


def restore_evidence(values: list[dict]) -> tuple[Evidence, ...]:
    """Restore retained server snapshots; browser input is never trusted here."""
    items = []
    try:
        for value in values:
            fields = dict(value)
            for key in ("evidence_id", "source_id", "revision_id"):
                fields[key] = UUID(fields[key])
            fields["indexed_at"] = datetime.fromisoformat(fields["indexed_at"])
            items.append(Evidence(**fields))
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise EvidenceUnavailableError("Retained evidence manifest is invalid") from exc
    return tuple(items)


class QueryService:
    def __init__(
        self,
        settings: Settings,
        repository: QueryRepository,
        provenance: ProvenanceService,
        lightrag: LightRAGRuntime,
        evidence_files: EvidenceAccess,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.provenance: ProvenanceService = provenance
        self.lightrag = lightrag
        self.evidence_files = evidence_files
        self.owner = uuid4()
        self._task: asyncio.Task | None = None

    async def enqueue(self, question: str) -> dict:
        return await self.repository.enqueue(validate_question(question))

    async def list_jobs(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        jobs = await self.repository.list_jobs(limit=limit, offset=offset)
        return [{key: value for key, value in job.items() if key != "result"} for job in jobs]

    async def get_job(self, job_id: UUID) -> dict | None:
        job = await self.repository.get_job(job_id)
        if job is None:
            return None
        result = job.get("result")
        if job.get("state") == "succeeded" and isinstance(result, dict):
            job = {**job, "result": await self._fresh_result(result)}
        return job

    async def detail(self, job_id: UUID) -> dict | None:
        """Public detail path; freshness is recalculated on every read."""
        return await self.get_job(job_id)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="knowgrain-query-jobs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def retry(self, job_id: UUID) -> dict:
        return await self.repository.retry(job_id)

    async def _fresh_result(self, result: dict) -> dict:
        evidence_values = result.get("evidence", [])
        try:
            evidence = restore_evidence(evidence_values)
        except EvidenceUnavailableError:
            malformed = [
                {**value, "current": False}
                for value in evidence_values if isinstance(value, dict)
            ] if isinstance(evidence_values, list) else []
            return {**result, "evidence": malformed, "evidence_current": False}

        current_by_id: dict[UUID, bool] = {}
        by_revision: dict[UUID, list[Evidence]] = {}
        for item in evidence:
            by_revision.setdefault(item.revision_id, []).append(item)
        for revision_items in by_revision.values():
            current = True
            try:
                await self.provenance.validate(tuple(revision_items))
            except EvidenceUnavailableError:
                current = False
            for item in revision_items:
                current_by_id[item.evidence_id] = current
        current_items = [
            {**item.snapshot(), "current": current_by_id[item.evidence_id]}
            for item in evidence
        ]
        return {
            **result,
            "evidence": current_items,
            "evidence_current": all(item["current"] for item in current_items),
        }

    async def _run(self) -> None:
        while True:
            if not self._dependencies_ready():
                await asyncio.sleep(1)
                continue
            try:
                job = await self.repository.claim_next(self.owner, lease_seconds=_LEASE_SECONDS)
                if job is not None:
                    await self._execute(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Query executor unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)

    def _dependencies_ready(self) -> bool:
        database = getattr(self.repository, "database", None)
        if database is not None and not getattr(database, "is_ready", False):
            return False
        if not getattr(self.lightrag, "is_ready", True):
            return False
        if getattr(self.lightrag, "restart_required", False):
            return False
        return getattr(self.lightrag, "model_validation_error", None) is None

    async def _execute(self, job: dict) -> None:
        job_id = UUID(str(job["job_id"]))
        processing = asyncio.create_task(self._process(job), name=f"query-{job_id}")
        lease = asyncio.create_task(self._renew(job_id), name=f"query-lease-{job_id}")
        try:
            done, _ = await asyncio.wait(
                {processing, lease}, return_when=asyncio.FIRST_COMPLETED
            )
            if lease in done:
                await lease
                raise QueryLeaseLostError("Query lease expired")
            await processing
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, EvidenceUnavailableError):
                error = "当前资料中没有可核验的有效证据；请检查资料或重新索引后重试"
            elif isinstance(exc, QueryValidationError):
                error = "模型回答不符合声明与引用规则；未保存结果，可重试"
            elif isinstance(exc, TimeoutError):
                error = "检索或模型回答超时；未保存结果，可重试"
            elif isinstance(exc, EvidenceFileError):
                error = "证据文件与保留版本不一致；检查 Vault 冲突后重试"
            elif isinstance(exc, QueryConflictError):
                error = "问答结果或证据版本发生冲突；检查任务后重试"
            else:
                error = f"问答失败 ({type(exc).__name__})；检查服务后重试"
            try:
                await self.repository.fail(job_id, self.owner, error)
            except Exception as record_error:
                logger.warning("Query failure recording failed (%s)", type(record_error).__name__)
        finally:
            processing.cancel()
            lease.cancel()
            # _publish_drained joins the file thread before processing exits.
            await asyncio.gather(processing, lease, return_exceptions=True)

    async def _renew(self, job_id: UUID) -> None:
        while True:
            await asyncio.sleep(_LEASE_RENEW_SECONDS)
            if not await self.repository.renew(
                job_id, self.owner, lease_seconds=_LEASE_SECONDS
            ):
                raise QueryLeaseLostError("Query lease was lost")

    async def _process(self, job: dict) -> None:
        job_id = UUID(str(job["job_id"]))
        try:
            async with asyncio.timeout(_RETRIEVAL_TIMEOUT_SECONDS):
                raw = await self.lightrag.retrieve(job["question"], mode="mix")
            evidence = await self.provenance.collect(raw)
        except EvidenceUnavailableError:
            await self._complete(job_id, self._insufficient_result(self._model_metadata()), ())
            return

        if not evidence:
            await self._complete(job_id, self._insufficient_result(self._model_metadata()), ())
            return

        system_prompt, prompt = build_query_prompt(job["question"], evidence)
        async with asyncio.timeout(_RETRIEVAL_TIMEOUT_SECONDS):
            response = await self.lightrag.generate_json(system_prompt, prompt)
        answer = parse_answer(response, evidence)
        model = self._model_metadata()

        if answer.status == "insufficient":
            await self._complete(job_id, self._insufficient_result(model), ())
            return

        cited_ids = {identity for claim in answer.claims for identity in claim.evidence_ids}
        cited = tuple(item for item in evidence if item.evidence_id in cited_ids)
        try:
            await self.provenance.validate(cited)
        except EvidenceUnavailableError:
            await self._complete(job_id, self._insufficient_result(model), ())
            return

        await self._publish_drained(cited)
        # Recheck after file publication and immediately before the transactional
        # repository completion. The repository repeats eligibility under locks.
        try:
            await self.provenance.validate(cited)
        except EvidenceUnavailableError:
            await self._complete(job_id, self._insufficient_result(model), ())
            return

        claims = [
            {
                "key": claim.key,
                "text": claim.text,
                "evidence_ids": [str(identity) for identity in claim.evidence_ids],
            }
            for claim in answer.claims
        ]
        result = {"status": "answered", "message": "", "claims": claims, "model": model}
        await self._complete(job_id, result, cited)

    async def _complete(
        self, job_id: UUID, result: dict, evidence: Sequence[Evidence]
    ) -> None:
        try:
            completed = await self.repository.complete(
                job_id, self.owner, result, tuple(evidence)
            )
        except EvidenceUnavailableError:
            # The database's final locked eligibility check can race the last
            # provenance read. Roll back the answer and retain an explicit result.
            if result.get("status") != "answered":
                raise
            completed = await self.repository.complete(
                job_id, self.owner, self._insufficient_result(result["model"]), ()
            )
        if not completed:
            raise QueryLeaseLostError("Query lease was lost before completion")

    async def _publish_drained(self, evidence: Sequence[Evidence]) -> None:
        task = asyncio.create_task(asyncio.to_thread(self.evidence_files.publish, evidence))
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
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
                return

    @staticmethod
    def _insufficient_result(model: dict) -> dict:
        return {
            "status": "insufficient",
            "message": INSUFFICIENT_MESSAGE,
            "claims": [],
            "model": model,
        }

    def _model_metadata(self) -> dict:
        return {
            "name": self.settings.llm_model,
            "provider": "ollama",
            "generated_at": datetime.now(UTC).isoformat(),
        }
