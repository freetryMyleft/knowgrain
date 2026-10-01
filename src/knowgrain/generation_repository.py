"""Transactional persistence for evidence-backed generation and review."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowgrain.database import ApplicationDatabase
from knowgrain.m3_types import Evidence, EvidenceUnavailableError, evidence_identity
from knowgrain.models import (
    EvidenceRef,
    GeneratedPage,
    GenerationJob,
    PageEvidence,
    SourceDocument,
    SourceRevision,
    WikiPage,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEASE_DURATION = timedelta(seconds=90)
_MAX_ERROR_LENGTH = 4000
_MAX_EVIDENCE_ITEMS = 24
_MAX_EXCERPT_LENGTH = 6000
_MAX_EVIDENCE_CHARACTERS = 48_000
_MAX_DRAFT_BYTES = 128 * 1024
_MAX_EVIDENCE_BYTES = 512 * 1024
_MAX_MODEL_BYTES = 64 * 1024
_SECRET_FIELD = re.compile(
    r"(^|_)(api_?key|access_?token|refresh_?token|secret|password|credential|authorization)(_|$)",
    re.IGNORECASE,
)


class GenerationConflictError(RuntimeError):
    """A generation/review operation no longer matches the durable state."""


class _LeaseExpired(RuntimeError):
    """Internal signal used to roll back work after a lease expires mid-transaction."""


class GenerationRepository:
    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    async def enqueue(
        self,
        topic: str,
        *,
        target_page_id: UUID | None = None,
        expected_target_sha256: str | None = None,
    ) -> dict:
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 2000:
            raise ValueError("topic must contain between 1 and 2000 characters")
        if (target_page_id is None) != (expected_target_sha256 is None):
            raise ValueError("target_page_id and expected_target_sha256 must be provided together")
        if expected_target_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            expected_target_sha256
        ):
            raise ValueError("expected_target_sha256 must be a lowercase SHA-256 digest")

        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            if target_page_id is not None:
                target = await session.scalar(
                    select(WikiPage)
                    .where(WikiPage.id == target_page_id)
                    .with_for_update()
                )
                if (
                    target is None
                    or not target.present
                    or target.content_sha256 != expected_target_sha256
                ):
                    raise GenerationConflictError("Proposal target changed; refresh the page")

            job = GenerationJob(
                id=uuid4(),
                topic=topic.strip(),
                target_page_id=target_page_id,
                target_sha256=expected_target_sha256,
                output_page_id=uuid4(),
                state="queued",
                phase="queued",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.flush()
            return self._job_snapshot(job)

    async def get_job(self, job_id: UUID) -> dict | None:
        async with self.database.session_factory() as session:
            job = await session.get(GenerationJob, job_id)
            return None if job is None else self._job_snapshot(job)

    async def list_jobs(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be zero or greater")
        async with self.database.session_factory() as session:
            jobs = (
                await session.scalars(
                    select(GenerationJob)
                    .order_by(GenerationJob.created_at.desc(), GenerationJob.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [self._job_snapshot(job) for job in jobs]

    async def claim(self, owner: UUID) -> dict | None:
        async with self.database.session_factory() as session, session.begin():
            statement = (
                select(GenerationJob)
                .where(
                    or_(
                        GenerationJob.state == "queued",
                        and_(
                            GenerationJob.state == "running",
                            or_(
                                GenerationJob.lease_until.is_(None),
                                GenerationJob.lease_until <= func.clock_timestamp(),
                            ),
                        ),
                    )
                )
                .order_by(GenerationJob.created_at, GenerationJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = await session.scalar(statement)
            if job is None:
                return None
            now = await session.scalar(select(func.clock_timestamp()))
            job.state = "running"
            job.phase = "projecting" if job.result is not None else "retrieving"
            job.lease_owner = owner
            job.lease_until = now + _LEASE_DURATION
            job.attempts += 1
            job.error = None
            job.updated_at = now
            await session.flush()
            return self._job_snapshot(job)

    async def renew(self, job_id: UUID, owner: UUID) -> bool:
        async with self.database.session_factory() as session, session.begin():
            job = await session.scalar(
                select(GenerationJob)
                .where(GenerationJob.id == job_id)
                .with_for_update()
            )
            if job is None or job.state != "running" or job.lease_owner != owner:
                return False
            now = await session.scalar(select(func.clock_timestamp()))
            if job.lease_until is None or job.lease_until <= now:
                return False
            result = await session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.state == "running",
                    GenerationJob.lease_owner == owner,
                    GenerationJob.lease_until > func.clock_timestamp(),
                )
                .values(lease_until=now + _LEASE_DURATION, updated_at=now)
            )
            return result.rowcount == 1

    async def release_owner(self, owner: UUID) -> None:
        async with self.database.session_factory() as session, session.begin():
            jobs = (
                await session.scalars(
                    select(GenerationJob)
                    .where(
                        GenerationJob.state == "running",
                        GenerationJob.lease_owner == owner,
                    )
                    .order_by(GenerationJob.id)
                    .with_for_update()
                )
            ).all()
            now = await session.scalar(select(func.clock_timestamp()))
            for job in jobs:
                job.state = "queued"
                job.phase = "projecting" if job.result is not None else "queued"
                job.lease_owner = None
                job.lease_until = None
                job.error = None
                job.updated_at = now

    async def store_result(
        self,
        job_id: UUID,
        owner: UUID,
        *,
        draft: dict,
        evidence: Sequence[Evidence],
        model: dict,
    ) -> bool:
        draft_value = self._bounded_json_object(draft, _MAX_DRAFT_BYTES, "draft")
        model_value = self._safe_model_metadata(model)
        evidence_items = self._normalize_evidence(evidence)
        evidence_snapshots = [item.snapshot() for item in evidence_items]
        self._bounded_json(evidence_snapshots, _MAX_EVIDENCE_BYTES, "evidence")
        expected_claims = self._claim_map_from_draft(draft_value)
        retained_evidence_ids = {item.evidence_id for item in evidence_items}
        if any(
            evidence_id not in retained_evidence_ids
            for claim_evidence_ids in expected_claims.values()
            for evidence_id in claim_evidence_ids
        ):
            raise GenerationConflictError("Retained draft cites evidence outside its manifest")
        result_value = {
            "draft": draft_value,
            "evidence": evidence_snapshots,
            "model": model_value,
        }
        self._bounded_json(result_value, _MAX_DRAFT_BYTES + _MAX_EVIDENCE_BYTES + _MAX_MODEL_BYTES, "result")
        async with self.database.session_factory() as session:
            try:
                async with session.begin():
                    job = await self._active_job(session, job_id, owner)
                    if job is None:
                        return False
                    await self._assert_current_evidence(session, evidence_items)

                    if job.result is not None:
                        if job.result != result_value:
                            raise GenerationConflictError("A retained generation result is immutable")
                        if not await self._update_live_lease(
                            session,
                            job_id,
                            owner,
                            phase="result_saved",
                            updated_at=func.clock_timestamp(),
                        ):
                            raise _LeaseExpired
                        return True

                    # All evidence is validated before inserting any row; the session transaction
                    # then retains the result and immutable evidence records atomically.
                    for item in evidence_items:
                        existing = await session.get(EvidenceRef, item.evidence_id)
                        if existing is not None:
                            if not self._same_evidence_row(existing, item):
                                raise GenerationConflictError(
                                    "Evidence identity already has different content"
                                )
                            continue
                        collision = await session.scalar(
                            select(EvidenceRef).where(
                                EvidenceRef.revision_id == item.revision_id,
                                EvidenceRef.chunk_id == item.chunk_id,
                                EvidenceRef.excerpt_sha256 == item.excerpt_sha256,
                            )
                        )
                        if collision is not None:
                            raise GenerationConflictError(
                                "Evidence identity conflicts with an existing reference"
                            )
                        session.add(self._evidence_row(item))

                    await session.flush()
                    if not await self._update_live_lease(
                        session,
                        job_id,
                        owner,
                        result=result_value,
                        phase="result_saved",
                        error=None,
                        updated_at=func.clock_timestamp(),
                    ):
                        raise _LeaseExpired
                    return True
            except _LeaseExpired:
                return False

    async def complete(
        self,
        job_id: UUID,
        owner: UUID,
        *,
        page_id: UUID,
        content_sha256: str,
        claims: Sequence[dict],
    ) -> bool:
        if not _SHA256_PATTERN.fullmatch(content_sha256):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        claim_map = self._claim_map(claims)
        async with self.database.session_factory() as session:
            try:
                async with session.begin():
                    job = await self._active_job(session, job_id, owner)
                    if job is None:
                        return False
                    if page_id != job.output_page_id:
                        raise GenerationConflictError(
                            "Projected page does not match the reserved output identity"
                        )
                    if not isinstance(job.result, dict) or set(job.result) != {
                        "draft", "evidence", "model"
                    }:
                        raise GenerationConflictError(
                            "Generation result must be retained before page projection"
                        )
                    retained_draft = job.result["draft"]
                    retained_evidence = job.result["evidence"]
                    retained_model = job.result["model"]
                    expected_claims = self._claim_map_from_draft(retained_draft)
                    if claim_map != expected_claims:
                        raise GenerationConflictError(
                            "Projected claims do not match the retained generation result"
                        )

                    page = await session.scalar(
                        select(WikiPage).where(WikiPage.id == page_id).with_for_update()
                    )
                    if (
                        page is None
                        or not page.present
                        or page.status != "draft"
                        or page.content_sha256 != content_sha256
                    ):
                        raise GenerationConflictError("Projected Wiki draft is missing or has changed")

                    evidence_items = self._evidence_from_snapshots(retained_evidence)
                    await self._assert_current_evidence(session, evidence_items, require_rows=True)
                    if await session.get(GeneratedPage, page_id) is not None:
                        raise GenerationConflictError("A generation manifest already exists for this page")

                    generated = GeneratedPage(
                        page_id=page_id,
                        generation_job_id=job.id,
                        draft=retained_draft,
                        generated_sha256=content_sha256,
                        proposal_target_page_id=job.target_page_id,
                        proposal_target_sha256=job.target_sha256,
                        model=retained_model,
                    )
                    session.add(generated)
                    for claim_key, evidence_ids in claim_map.items():
                        for evidence_id in evidence_ids:
                            session.add(
                                PageEvidence(
                                    page_id=page_id,
                                    evidence_id=evidence_id,
                                    claim_key=claim_key,
                                )
                            )
                    await session.flush()
                    if not await self._update_live_lease(
                        session,
                        job_id,
                        owner,
                        state="succeeded",
                        phase="completed",
                        output_sha256=content_sha256,
                        lease_owner=None,
                        lease_until=None,
                        error=None,
                        updated_at=func.clock_timestamp(),
                    ):
                        raise _LeaseExpired
                    return True
            except _LeaseExpired:
                return False

    async def fail(self, job_id: UUID, owner: UUID, error: str) -> bool:
        safe_error = self._safe_error(error)
        async with self.database.session_factory() as session, session.begin():
            job = await self._active_job(session, job_id, owner)
            if job is None:
                return False
            return await self._update_live_lease(
                session,
                job_id,
                owner,
                state="failed",
                phase="failed",
                error=safe_error,
                lease_owner=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )

    async def retry(self, job_id: UUID) -> dict:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            job = await session.scalar(
                select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
            )
            if job is None:
                raise GenerationConflictError("Generation job does not exist")
            if job.state != "failed":
                raise GenerationConflictError("Only failed generation jobs can be retried")
            job.state = "queued"
            job.phase = "projecting" if job.result is not None else "queued"
            job.lease_owner = None
            job.lease_until = None
            job.error = None
            job.updated_at = now
            await session.flush()
            return self._job_snapshot(job)

    async def get_generation(self, page_id: UUID) -> dict | None:
        async with self.database.session_factory() as session:
            generation = await session.get(GeneratedPage, page_id)
            if generation is None:
                return None
            job = await session.get(GenerationJob, generation.generation_job_id)
            if job is None:
                return None
            return {
                "page_id": str(generation.page_id),
                "job_id": str(generation.generation_job_id),
                "draft": generation.draft,
                "evidence": (job.result or {}).get("evidence"),
                "model": generation.model,
                "generated_sha256": generation.generated_sha256,
                "proposal_target_page_id": str(generation.proposal_target_page_id)
                if generation.proposal_target_page_id
                else None,
                "proposal_target_sha256": generation.proposal_target_sha256,
                "reviewed_at": self._iso(generation.reviewed_at),
                "reviewed_sha256": generation.reviewed_sha256,
                "created_at": self._iso(generation.created_at),
            }

    async def get_evidence(self, evidence_id: UUID) -> Evidence | None:
        """Return one retained quotation for the review/evidence viewer."""
        async with self.database.session_factory() as session:
            row = await session.get(EvidenceRef, evidence_id)
            if row is None:
                return None
            evidence = Evidence(
                evidence_id=row.evidence_id,
                source_id=row.source_id,
                revision_id=row.revision_id,
                filename=row.filename,
                vault_path=row.vault_path,
                source_sha256=row.source_sha256,
                parsed_text_sha256=row.parsed_text_sha256,
                chunk_id=row.chunk_id,
                excerpt=row.excerpt,
                excerpt_sha256=row.excerpt_sha256,
                start=row.start,
                end=row.end,
                page=row.page,
                heading=row.heading,
                indexed_at=row.indexed_at,
            )
            self._normalize_evidence([evidence])
            return evidence

    async def record_review(
        self,
        page_id: UUID,
        *,
        expected_generated_sha256: str,
        reviewed_sha256: str,
    ) -> None:
        if not _SHA256_PATTERN.fullmatch(expected_generated_sha256):
            raise ValueError("expected_generated_sha256 must be a lowercase SHA-256 digest")
        if not _SHA256_PATTERN.fullmatch(reviewed_sha256):
            raise ValueError("reviewed_sha256 must be a lowercase SHA-256 digest")
        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            generation = await session.scalar(
                select(GeneratedPage)
                .where(GeneratedPage.page_id == page_id)
                .with_for_update()
            )
            if generation is None:
                raise GenerationConflictError("Generated page does not exist")
            if generation.generated_sha256 != expected_generated_sha256:
                raise GenerationConflictError("Generated manifest hash changed; review again from a current draft")

            page = await session.scalar(
                select(WikiPage).where(WikiPage.id == page_id).with_for_update()
            )
            if (
                page is None
                or not page.present
                or page.status != "reviewed"
                or page.content_sha256 != reviewed_sha256
            ):
                raise GenerationConflictError("Reviewed Wiki projection does not match the reviewed file hash")
            job = await session.get(GenerationJob, generation.generation_job_id)
            if (
                job is None
                or not isinstance(job.result, dict)
                or job.result.get("draft") != generation.draft
                or not isinstance(job.result.get("evidence"), list)
            ):
                raise GenerationConflictError("Retained generation manifest is incomplete")
            evidence_items = self._evidence_from_snapshots(job.result["evidence"])
            await self._assert_current_evidence(session, evidence_items, require_rows=True)
            if generation.reviewed_at is not None:
                if generation.reviewed_sha256 == reviewed_sha256:
                    return
                raise GenerationConflictError("Generated page was already reviewed with another hash")
            generation.reviewed_at = now
            generation.reviewed_sha256 = reviewed_sha256
            await session.flush()

    @staticmethod
    async def _active_job(
        session: AsyncSession, job_id: UUID, owner: UUID
    ) -> GenerationJob | None:
        job = await session.scalar(
            select(GenerationJob)
            .where(GenerationJob.id == job_id)
            .with_for_update()
        )
        if (
            job is None
            or job.state != "running"
            or job.lease_owner != owner
            or job.lease_until is None
        ):
            return None
        database_now = await session.scalar(select(func.clock_timestamp()))
        if job.lease_until <= database_now:
            return None
        return job

    @staticmethod
    async def _update_live_lease(
        session: AsyncSession,
        job_id: UUID,
        owner: UUID,
        **values: Any,
    ) -> bool:
        result = await session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.state == "running",
                GenerationJob.lease_owner == owner,
                GenerationJob.lease_until > func.clock_timestamp(),
            )
            .values(**values)
        )
        return result.rowcount == 1

    async def _assert_current_evidence(
        self,
        session: AsyncSession,
        evidence: Sequence[Evidence],
        *,
        require_rows: bool = False,
    ) -> None:
        if not evidence or len(evidence) > _MAX_EVIDENCE_ITEMS:
            raise EvidenceUnavailableError("Evidence manifest is empty or exceeds its item limit")

        by_id = {item.evidence_id: item for item in evidence}
        if len(by_id) != len(evidence):
            raise EvidenceUnavailableError("Evidence manifest contains duplicate identities")
        source_ids = sorted({item.source_id for item in evidence}, key=str)
        revision_ids = sorted({item.revision_id for item in evidence}, key=str)

        sources = (
            await session.scalars(
                select(SourceDocument)
                .where(SourceDocument.id.in_(source_ids))
                .order_by(SourceDocument.id)
                .with_for_update()
            )
        ).all()
        revisions = (
            await session.scalars(
                select(SourceRevision)
                .where(SourceRevision.id.in_(revision_ids))
                .order_by(SourceRevision.source_id, SourceRevision.id)
                .with_for_update()
            )
        ).all()
        source_by_id = {source.id: source for source in sources}
        revision_by_id = {revision.id: revision for revision in revisions}

        for item in evidence:
            source = source_by_id.get(item.source_id)
            revision = revision_by_id.get(item.revision_id)
            if (
                source is None
                or revision is None
                or source.state != "active"
                or source.current_revision_id != item.revision_id
                or source.latest_revision_id != item.revision_id
                or revision.source_id != item.source_id
                or revision.index_state != "ready"
                or revision.indexed_at is None
                or revision.parsed_text_sha256 is None
                or revision.filename != item.filename
                or revision.vault_path != item.vault_path
                or revision.sha256 != item.source_sha256
                or revision.parsed_text_sha256 != item.parsed_text_sha256
                or revision.indexed_at != item.indexed_at
            ):
                raise EvidenceUnavailableError("Evidence no longer refers to the current indexed revision")

        if require_rows:
            stored = (
                await session.scalars(
                    select(EvidenceRef)
                    .where(EvidenceRef.evidence_id.in_(list(by_id)))
                    .order_by(EvidenceRef.evidence_id)
                    .with_for_update()
                )
            ).all()
            stored_by_id = {row.evidence_id: row for row in stored}
            if len(stored_by_id) != len(by_id) or any(
                not self._same_evidence_row(stored_by_id.get(item.evidence_id), item)
                for item in evidence
            ):
                raise EvidenceUnavailableError("Retained evidence manifest is missing or changed")

    @classmethod
    def _normalize_evidence(cls, evidence: Sequence[Evidence]) -> list[Evidence]:
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise ValueError("evidence must be a sequence of verified Evidence records")
        items = list(evidence)
        if not 1 <= len(items) <= _MAX_EVIDENCE_ITEMS:
            raise EvidenceUnavailableError("Evidence manifest is empty or exceeds its item limit")
        if any(not isinstance(item, Evidence) for item in items):
            raise ValueError("evidence must contain verified Evidence records")
        total_characters = 0
        for item in items:
            if (
                not isinstance(item.evidence_id, UUID)
                or not isinstance(item.source_id, UUID)
                or not isinstance(item.revision_id, UUID)
                or not isinstance(item.filename, str)
                or not item.filename
                or len(item.filename) > 1024
                or not isinstance(item.vault_path, str)
                or not item.vault_path
                or len(item.vault_path) > 2048
                or not isinstance(item.chunk_id, str)
                or not item.chunk_id
                or "\x00" in item.chunk_id
                or len(item.chunk_id) > 512
                or not isinstance(item.excerpt, str)
                or not item.excerpt
                or "\x00" in item.excerpt
                or len(item.excerpt) > _MAX_EXCERPT_LENGTH
                or isinstance(item.start, bool)
                or not isinstance(item.start, int)
                or isinstance(item.end, bool)
                or not isinstance(item.end, int)
                or item.start < 0
                or item.end <= item.start
                or len(item.excerpt) != item.end - item.start
                or (
                    item.page is not None
                    and (isinstance(item.page, bool) or not isinstance(item.page, int) or item.page < 1)
                )
                or (
                    item.heading is not None
                    and (not isinstance(item.heading, str) or "\x00" in item.heading)
                )
                or not cls._valid_hash(item.source_sha256)
                or not cls._valid_hash(item.parsed_text_sha256)
                or not cls._valid_hash(item.excerpt_sha256)
                or hashlib.sha256(item.excerpt.encode("utf-8")).hexdigest() != item.excerpt_sha256
                or evidence_identity(item.revision_id, item.chunk_id, item.excerpt_sha256)
                != item.evidence_id
                or not isinstance(item.indexed_at, datetime)
                or item.indexed_at.tzinfo is None
                or item.indexed_at.utcoffset() is None
            ):
                raise EvidenceUnavailableError("Evidence record failed its identity or integrity checks")
            total_characters += len(item.excerpt)
        if total_characters > _MAX_EVIDENCE_CHARACTERS:
            raise EvidenceUnavailableError("Evidence manifest exceeds the total excerpt limit")
        if len({item.evidence_id for item in items}) != len(items):
            raise EvidenceUnavailableError("Evidence manifest contains duplicate identities")
        return items

    @classmethod
    def _evidence_from_snapshots(cls, snapshots: Any) -> list[Evidence]:
        if not isinstance(snapshots, list):
            raise EvidenceUnavailableError("Retained evidence manifest is malformed")
        try:
            evidence = [
                Evidence(
                    evidence_id=UUID(item["evidence_id"]),
                    source_id=UUID(item["source_id"]),
                    revision_id=UUID(item["revision_id"]),
                    filename=item["filename"],
                    vault_path=item["vault_path"],
                    source_sha256=item["source_sha256"],
                    parsed_text_sha256=item["parsed_text_sha256"],
                    chunk_id=item["chunk_id"],
                    excerpt=item["excerpt"],
                    excerpt_sha256=item["excerpt_sha256"],
                    start=item["start"],
                    end=item["end"],
                    page=item.get("page"),
                    heading=item.get("heading"),
                    indexed_at=datetime.fromisoformat(item["indexed_at"]),
                )
                for item in snapshots
            ]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise EvidenceUnavailableError("Retained evidence manifest is malformed") from exc
        return cls._normalize_evidence(evidence)

    @staticmethod
    def _evidence_row(item: Evidence) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=item.evidence_id,
            source_id=item.source_id,
            revision_id=item.revision_id,
            filename=item.filename,
            vault_path=item.vault_path,
            source_sha256=item.source_sha256,
            parsed_text_sha256=item.parsed_text_sha256,
            chunk_id=item.chunk_id,
            excerpt=item.excerpt,
            excerpt_sha256=item.excerpt_sha256,
            start=item.start,
            end=item.end,
            page=item.page,
            heading=item.heading,
            indexed_at=item.indexed_at,
        )

    @staticmethod
    def _same_evidence_row(row: EvidenceRef | None, item: Evidence) -> bool:
        return row is not None and all(
            getattr(row, field) == getattr(item, field)
            for field in (
                "evidence_id",
                "source_id",
                "revision_id",
                "filename",
                "vault_path",
                "source_sha256",
                "parsed_text_sha256",
                "chunk_id",
                "excerpt",
                "excerpt_sha256",
                "start",
                "end",
                "page",
                "heading",
                "indexed_at",
            )
        )

    @classmethod
    def _claim_map(cls, claims: Sequence[dict]) -> dict[str, tuple[UUID, ...]]:
        if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
            raise ValueError("claims must be a sequence")
        result: dict[str, tuple[UUID, ...]] = {}
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {"key", "evidence_ids"}:
                raise ValueError("each claim mapping must contain only key and evidence_ids")
            key = claim["key"]
            values = claim["evidence_ids"]
            if not isinstance(key, str) or not key.strip() or len(key) > 64 or key in result:
                raise ValueError("claim keys must be unique nonempty identifiers")
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ValueError("claim evidence_ids must be a sequence")
            try:
                evidence_ids = tuple(UUID(str(value)) for value in values)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("claim evidence_ids must contain UUIDs") from exc
            if not 1 <= len(evidence_ids) <= 6 or len(set(evidence_ids)) != len(evidence_ids):
                raise ValueError("each claim must cite between 1 and 6 unique evidence items")
            result[key] = evidence_ids
        if not 1 <= len(result) <= 48:
            raise ValueError("a generated page must contain between 1 and 48 claims")
        return result

    @classmethod
    def _claim_map_from_draft(cls, draft: Any) -> dict[str, tuple[UUID, ...]]:
        if not isinstance(draft, dict) or not isinstance(draft.get("sections"), list):
            raise GenerationConflictError("Retained draft has no claim sections")
        claims: list[dict] = []
        for section in draft["sections"]:
            if not isinstance(section, dict) or not isinstance(section.get("claims"), list):
                raise GenerationConflictError("Retained draft claim structure is malformed")
            for claim in section["claims"]:
                if not isinstance(claim, dict) or not {"key", "evidence_ids"} <= set(claim):
                    raise GenerationConflictError("Retained draft claim structure is malformed")
                claims.append({"key": claim["key"], "evidence_ids": claim["evidence_ids"]})
        try:
            return cls._claim_map(claims)
        except ValueError as exc:
            raise GenerationConflictError("Retained draft claims failed consistency checks") from exc

    @classmethod
    def _safe_model_metadata(cls, value: dict) -> dict:
        result = cls._bounded_json_object(value, _MAX_MODEL_BYTES, "model")
        required = ("name", "provider", "generated_at")
        if any(not isinstance(result.get(key), str) or not result[key].strip() for key in required):
            raise ValueError("model metadata must include name, provider, and generated_at")
        stack: list[Any] = [result]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, nested in current.items():
                    normalized_key = re.sub(r"[^a-z]", "", key.casefold())
                    has_secret_suffix = normalized_key.endswith(
                        ("key", "token", "secret", "password", "credential", "authorization")
                    )
                    if _SECRET_FIELD.search(key) or has_secret_suffix:
                        raise ValueError("model metadata cannot contain credential fields")
                    stack.append(nested)
            elif isinstance(current, list):
                stack.extend(current)
        return result

    @staticmethod
    def _bounded_json_object(value: Any, maximum_bytes: int, field: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a JSON object")
        normalized = GenerationRepository._bounded_json(value, maximum_bytes, field)
        return normalized

    @staticmethod
    def _bounded_json(value: Any, maximum_bytes: int, field: str) -> Any:
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            normalized = json.loads(encoded)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError(f"{field} must contain only finite JSON values") from exc
        if len(encoded) > maximum_bytes:
            raise ValueError(f"{field} exceeds its storage size limit")
        stack: list[Any] = [normalized]
        while stack:
            current = stack.pop()
            if isinstance(current, str):
                if "\x00" in current:
                    raise ValueError(f"{field} cannot contain NUL characters")
            elif isinstance(current, dict):
                if any("\x00" in key for key in current):
                    raise ValueError(f"{field} cannot contain NUL characters")
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return normalized

    @staticmethod
    def _valid_hash(value: Any) -> bool:
        return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None

    @staticmethod
    def _safe_error(error: str) -> str:
        if not isinstance(error, str):
            error = "Generation failed"
        cleaned = "".join(character for character in error if character >= " " or character in "\t\n")
        cleaned = " ".join(cleaned.split())
        return (cleaned or "Generation failed")[:_MAX_ERROR_LENGTH]

    @classmethod
    def _job_snapshot(cls, job: GenerationJob) -> dict:
        return {
            "job_id": str(job.id),
            "topic": job.topic,
            "target_page_id": str(job.target_page_id) if job.target_page_id else None,
            "expected_target_sha256": job.target_sha256,
            "output_page_id": str(job.output_page_id),
            "state": job.state,
            "phase": job.phase,
            "attempts": job.attempts,
            "result": job.result,
            "error": job.error,
            "output_sha256": job.output_sha256,
            "created_at": cls._iso(job.created_at),
            "updated_at": cls._iso(job.updated_at),
        }

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
