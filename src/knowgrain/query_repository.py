"""Durable question jobs and immutable, evidence-backed answer snapshots."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowgrain.database import ApplicationDatabase
from knowgrain.generation_repository import GenerationRepository
from knowgrain.m3_types import Evidence, EvidenceUnavailableError
from knowgrain.models import EvidenceRef, QueryJob
from knowgrain.query_contract import QueryValidationError, parse_answer


_FIXED_INSUFFICIENT_MESSAGE = "无法核实：当前资料不足以支持该问题。"
_MAX_RESULT_BYTES = 768 * 1024
_MAX_ERROR_LENGTH = 4000
_MAX_LEASE_SECONDS = 3600


class QueryConflictError(RuntimeError):
    """A question job is in a state that no longer permits this operation."""


class _LeaseExpired(RuntimeError):
    """Roll back a completion whose worker lease expired during row locking."""


class QueryRepository:
    """Persist query lifecycle state without retaining unverified source material."""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database
        self._generation_repository = GenerationRepository(database)

    async def enqueue(self, question: str) -> dict[str, Any]:
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question.strip()) > 1000
            or "\x00" in question
        ):
            raise ValueError("question must contain between 1 and 1000 characters")
        question = question.strip()
        async with self.database.session_factory() as session, session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            job = QueryJob(
                id=uuid4(),
                question=question,
                state="queued",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.flush()
            return self._job_snapshot(job)

    async def list_jobs(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be zero or greater")
        async with self.database.session_factory() as session:
            jobs = (
                await session.scalars(
                    select(QueryJob)
                    .order_by(QueryJob.created_at.desc(), QueryJob.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            # Query history lists contain status only; retained quotes and answers
            # are fetched through the single-job endpoint.
            return [
                {
                    key: value
                    for key, value in self._job_snapshot(job).items()
                    if key != "result"
                }
                for job in jobs
            ]

    async def get_job(self, job_id: UUID) -> dict[str, Any] | None:
        self._validate_id(job_id)
        async with self.database.session_factory() as session:
            job = await session.get(QueryJob, job_id)
            return None if job is None else self._job_snapshot(job)

    async def claim_next(self, owner: UUID, lease_seconds: int = 90) -> dict[str, Any] | None:
        self._validate_id(owner, "owner")
        lease = self._lease_delta(lease_seconds)
        async with self.database.session_factory() as session, session.begin():
            job = await session.scalar(
                select(QueryJob)
                .where(
                    or_(
                        QueryJob.state == "queued",
                        and_(
                            QueryJob.state == "running",
                            or_(
                                QueryJob.lease_until.is_(None),
                                QueryJob.lease_until <= func.clock_timestamp(),
                            ),
                        ),
                    )
                )
                .order_by(QueryJob.created_at, QueryJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            # Re-evaluate with database time after FOR UPDATE has acquired the row;
            # a queued job can also have become a live lease while this query waited.
            now = await session.scalar(select(func.clock_timestamp()))
            if job.state == "running" and job.lease_until is not None and job.lease_until > now:
                return None
            job.state = "running"
            job.lease_owner = owner
            job.lease_until = now + lease
            job.attempts += 1
            job.error = None
            job.updated_at = now
            await session.flush()
            return self._job_snapshot(job)

    async def renew(self, job_id: UUID, owner: UUID, lease_seconds: int = 90) -> bool:
        self._validate_id(job_id)
        self._validate_id(owner, "owner")
        lease = self._lease_delta(lease_seconds)
        async with self.database.session_factory() as session, session.begin():
            job = await session.scalar(
                select(QueryJob).where(QueryJob.id == job_id).with_for_update()
            )
            if job is None or job.state != "running" or job.lease_owner != owner:
                return False
            now = await session.scalar(select(func.clock_timestamp()))
            if job.lease_until is None or job.lease_until <= now:
                return False
            result = await session.execute(
                update(QueryJob)
                .where(
                    QueryJob.id == job_id,
                    QueryJob.state == "running",
                    QueryJob.lease_owner == owner,
                    QueryJob.lease_until > func.clock_timestamp(),
                )
                .values(lease_until=now + lease, updated_at=now)
            )
            return result.rowcount == 1

    async def complete(
        self,
        job_id: UUID,
        owner: UUID,
        result: dict,
        evidence: Sequence[Evidence],
    ) -> bool:
        self._validate_id(job_id)
        self._validate_id(owner, "owner")
        evidence_items, result_value = self._normalize_result(result, evidence)
        try:
            async with self.database.session_factory() as session:
                try:
                    async with session.begin():
                        job = await self._active_job(session, job_id, owner)
                        if job is None:
                            return False
                        if evidence_items:
                            await self._generation_repository._assert_current_evidence(
                                session, evidence_items
                            )
                            await self._retain_evidence(session, evidence_items)

                        # Source/revision/evidence locks may have waited past lease expiry.
                        now = await session.scalar(select(func.clock_timestamp()))
                        if job.lease_until is None or job.lease_until <= now:
                            raise _LeaseExpired
                        if job.result is not None:
                            if job.result != result_value:
                                raise QueryConflictError("A successful query result is immutable")
                            raise QueryConflictError("Query job is already complete")
                        updated = await session.execute(
                            update(QueryJob)
                            .where(
                                QueryJob.id == job_id,
                                QueryJob.state == "running",
                                QueryJob.lease_owner == owner,
                                QueryJob.lease_until > func.clock_timestamp(),
                            )
                            .values(
                                state="succeeded",
                                result=result_value,
                                error=None,
                                lease_owner=None,
                                lease_until=None,
                                updated_at=func.clock_timestamp(),
                            )
                        )
                        if updated.rowcount != 1:
                            raise _LeaseExpired
                    return True
                except _LeaseExpired:
                    return False
        except IntegrityError as exc:
            raise QueryConflictError("Evidence conflicts with an existing immutable reference") from exc

    async def fail(self, job_id: UUID, owner: UUID, error: str) -> bool:
        self._validate_id(job_id)
        self._validate_id(owner, "owner")
        safe_error = self._safe_error(error)
        async with self.database.session_factory() as session, session.begin():
            job = await self._active_job(session, job_id, owner)
            if job is None:
                return False
            result = await session.execute(
                update(QueryJob)
                .where(
                    QueryJob.id == job_id,
                    QueryJob.state == "running",
                    QueryJob.lease_owner == owner,
                    QueryJob.lease_until > func.clock_timestamp(),
                )
                .values(
                    state="failed",
                    error=safe_error,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=func.clock_timestamp(),
                )
            )
            return result.rowcount == 1

    async def retry(self, job_id: UUID) -> dict[str, Any]:
        self._validate_id(job_id)
        async with self.database.session_factory() as session, session.begin():
            job = await session.scalar(
                select(QueryJob).where(QueryJob.id == job_id).with_for_update()
            )
            if job is None:
                raise QueryConflictError("Query job does not exist")
            if job.state != "failed":
                raise QueryConflictError("Only failed query jobs can be retried")
            now = await session.scalar(select(func.clock_timestamp()))
            job.state = "queued"
            job.lease_owner = None
            job.lease_until = None
            job.error = None
            job.updated_at = now
            await session.flush()
            return self._job_snapshot(job)

    async def _active_job(
        self, session: AsyncSession, job_id: UUID, owner: UUID
    ) -> QueryJob | None:
        job = await session.scalar(
            select(QueryJob).where(QueryJob.id == job_id).with_for_update()
        )
        if (
            job is None
            or job.state != "running"
            or job.lease_owner != owner
            or job.lease_until is None
        ):
            return None
        now = await session.scalar(select(func.clock_timestamp()))
        return job if job.lease_until > now else None

    async def _retain_evidence(
        self, session: AsyncSession, evidence: Sequence[Evidence]
    ) -> None:
        by_id = {item.evidence_id: item for item in evidence}
        rows = (
            await session.scalars(
                select(EvidenceRef)
                .where(EvidenceRef.evidence_id.in_(list(by_id)))
                .order_by(EvidenceRef.evidence_id)
                .with_for_update()
            )
        ).all()
        stored_by_id = {row.evidence_id: row for row in rows}
        for item in evidence:
            existing = stored_by_id.get(item.evidence_id)
            if existing is not None:
                if not GenerationRepository._same_evidence_row(existing, item):
                    raise QueryConflictError("Evidence identity already has different content")
                continue
            collision = await session.scalar(
                select(EvidenceRef)
                .where(
                    EvidenceRef.revision_id == item.revision_id,
                    EvidenceRef.chunk_id == item.chunk_id,
                    EvidenceRef.excerpt_sha256 == item.excerpt_sha256,
                )
                .with_for_update()
            )
            if collision is not None:
                raise QueryConflictError("Evidence identity conflicts with an existing reference")
            session.add(GenerationRepository._evidence_row(item))
        await session.flush()

    @classmethod
    def _normalize_result(
        cls, result: dict, evidence: Sequence[Evidence]
    ) -> tuple[list[Evidence], dict[str, Any]]:
        if not isinstance(result, dict) or set(result) != {"status", "message", "claims", "model"}:
            raise ValueError("query result must contain status, message, claims, and model")
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise ValueError("evidence must be a sequence of verified Evidence records")
        evidence_items = list(evidence)
        if any(not isinstance(item, Evidence) for item in evidence_items):
            raise ValueError("evidence must contain verified Evidence records")

        status = result["status"]
        message = result["message"]
        claims = result["claims"]
        if not isinstance(status, str) or status not in {"answered", "insufficient"}:
            raise ValueError("query result has an invalid status")
        if not isinstance(message, str) or len(message) > 2000 or "\x00" in message:
            raise ValueError("query result message exceeds safe bounds")
        if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
            raise ValueError("query result claims must be a sequence")
        if status == "insufficient":
            if claims or evidence_items or message != _FIXED_INSUFFICIENT_MESSAGE:
                raise ValueError("insufficient query results must have the fixed message and no evidence")
            evidence_items = []
        else:
            if message != "":
                raise ValueError("answered query results require an empty message")
            evidence_items = GenerationRepository._normalize_evidence(evidence_items)
            evidence_ids = {item.evidence_id for item in evidence_items}
            if not evidence_ids:
                raise EvidenceUnavailableError("Answered results require verified evidence")
        try:
            answer_value = json.dumps(
                {"status": status, "claims": claims},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            answer = parse_answer(answer_value, evidence_items)
        except (TypeError, ValueError, QueryValidationError) as exc:
            raise ValueError("query result claims failed the answer contract") from exc
        claims_value = [
            {
                "key": claim.key,
                "text": claim.text,
                "evidence_ids": [str(identity) for identity in claim.evidence_ids],
            }
            for claim in answer.claims
        ]
        cited = {identity for claim in answer.claims for identity in claim.evidence_ids}
        evidence_ids = {item.evidence_id for item in evidence_items}
        if status == "answered" and cited != evidence_ids:
            raise EvidenceUnavailableError(
                "Every retained answer evidence item must be cited, and every citation must be retained"
            )

        model = GenerationRepository._safe_model_metadata(result["model"])
        snapshots = [item.snapshot() for item in evidence_items]
        result_value = {
            "status": status,
            "message": message,
            "claims": claims_value,
            "evidence": snapshots,
            "model": model,
        }
        encoded = json.dumps(result_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("query result exceeds its storage limit")
        return evidence_items, result_value

    @staticmethod
    def _safe_error(error: str) -> str:
        if not isinstance(error, str):
            error = "Query processing failed"
        safe = " ".join(
            "".join(
                char
                for char in error
                if unicodedata.category(char) not in {"Cc", "Cf"}
            ).split()
        )
        return safe[:_MAX_ERROR_LENGTH] or "Query processing failed"

    @staticmethod
    def _job_snapshot(job: QueryJob) -> dict[str, Any]:
        return {
            "job_id": str(job.id),
            "question": job.question,
            "state": job.state,
            "attempts": job.attempts,
            "error": job.error,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "lease_until": job.lease_until.isoformat() if job.lease_until else None,
            "result": job.result,
        }

    @staticmethod
    def _validate_id(value: UUID, field: str = "job_id") -> None:
        if not isinstance(value, UUID):
            raise ValueError(f"{field} must be a UUID")

    @staticmethod
    def _lease_delta(value: int) -> timedelta:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= _MAX_LEASE_SECONDS
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        return timedelta(seconds=value)
