from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from knowgrain.database import ApplicationDatabase
from knowgrain.models import Job, SourceDocument, SourceRevision


PersistSource = Callable[[uuid.UUID, uuid.UUID], Awaitable[str]]
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEASE_DURATION = timedelta(seconds=90)
_MAX_ERROR_LENGTH = 4000


@dataclass(frozen=True, slots=True)
class ImportResult:
    source_id: uuid.UUID
    revision_id: uuid.UUID
    job_id: uuid.UUID
    duplicate: bool
    vault_path: str


class SourceNotFoundError(LookupError):
    pass


class SourceConflictError(RuntimeError):
    pass


class SourceRepository:
    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    async def register_source(
        self,
        filename: str,
        sha256: str,
        media_type: str,
        persist: PersistSource,
        source_id: uuid.UUID | None = None,
    ) -> ImportResult:
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        if not filename or len(filename) > 1024:
            raise ValueError("filename must contain between 1 and 1024 characters")
        if not media_type or len(media_type) > 255:
            raise ValueError("media_type must contain between 1 and 255 characters")

        async with self.database.session_factory() as session, session.begin():
            if source_id is None:
                await self._lock_hash(session, sha256)
                existing = await self._find_active_hash(session, sha256)
                if existing is not None:
                    source, revision, job = existing
                    return ImportResult(
                        source.id, revision.id, job.id, True, revision.vault_path
                    )
                source_id = uuid.uuid4()
                source = SourceDocument(id=source_id, filename=filename, state="active")
                session.add(source)
                await session.flush()
            else:
                await self._lock_hash(session, sha256)
                source = await session.scalar(
                    select(SourceDocument)
                    .where(SourceDocument.id == source_id)
                    .with_for_update()
                )
                if source is None:
                    raise SourceNotFoundError(f"Source {source_id} does not exist")
                if source.state != "active":
                    raise SourceConflictError("A deleted source cannot receive a new revision")
                existing_revision = await session.scalar(
                    select(SourceRevision).where(
                        SourceRevision.source_id == source_id,
                        SourceRevision.sha256 == sha256,
                    )
                )
                if existing_revision is not None:
                    job = await self._job_for_revision(session, existing_revision.id)
                    return ImportResult(
                        source.id,
                        existing_revision.id,
                        job.id,
                        True,
                        existing_revision.vault_path,
                    )

            revision_id = uuid.uuid4()
            vault_path = await persist(source_id, revision_id)
            if not isinstance(vault_path, str) or not vault_path or len(vault_path) > 2048:
                raise ValueError("persist callback must return a Vault-relative path")

            revision = SourceRevision(
                id=revision_id,
                source_id=source_id,
                filename=filename,
                sha256=sha256,
                vault_path=vault_path,
                media_type=media_type,
                index_state="queued",
            )
            session.add(revision)
            await session.flush()
            job_id = uuid.uuid4()
            job = Job(id=job_id, kind="index", revision_id=revision_id, state="queued")
            session.add(job)
            await session.flush()
            source.filename = filename
            source.latest_revision_id = revision_id
            await session.flush()
            return ImportResult(source_id, revision_id, job_id, False, vault_path)

    async def list_sources(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be zero or greater")

        latest_revision = aliased(SourceRevision, name="latest_revision")
        current_revision = aliased(SourceRevision, name="current_revision")
        async with self.database.session_factory() as session:
            statement = (
                select(SourceDocument, latest_revision, current_revision)
                .outerjoin(
                    latest_revision,
                    latest_revision.id == SourceDocument.latest_revision_id,
                )
                .outerjoin(
                    current_revision,
                    current_revision.id == SourceDocument.current_revision_id,
                )
                .order_by(SourceDocument.created_at.desc(), SourceDocument.id)
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(statement)).all()
            return [
                self._source_snapshot_for_revisions(source, latest, current)
                for source, latest, current in rows
            ]

    async def get_source(self, source_id: uuid.UUID) -> dict | None:
        async with self.database.session_factory() as session:
            source = await session.get(SourceDocument, source_id)
            if source is None:
                return None
            return await self._source_snapshot(session, source)

    async def get_revision(self, revision_id: uuid.UUID) -> dict | None:
        async with self.database.session_factory() as session:
            revision = await session.get(SourceRevision, revision_id)
            if revision is None:
                return None
            source = await session.get(SourceDocument, revision.source_id)
            if source is None:
                return None
            snapshot = self._revision_snapshot(revision)
            return {
                "source_id": str(source.id),
                "filename": revision.filename,
                "vault_path": revision.vault_path,
                "sha256": revision.sha256,
                "media_type": revision.media_type,
                "state": revision.index_state,
                "index_state": revision.index_state,
                "error": revision.error,
                **snapshot,
            }

    async def get_job(self, job_id: uuid.UUID) -> dict | None:
        async with self.database.session_factory() as session:
            job = await session.get(Job, job_id)
            return None if job is None else self._job_snapshot(job)

    async def retry_source(self, source_id: uuid.UUID) -> uuid.UUID:
        async with self.database.session_factory() as session, session.begin():
            source = await session.scalar(
                select(SourceDocument).where(SourceDocument.id == source_id).with_for_update()
            )
            if source is None:
                raise SourceNotFoundError(f"Source {source_id} does not exist")
            if source.state != "active":
                raise SourceConflictError("A deleted source cannot be reindexed")
            if source.latest_revision_id is None:
                raise SourceConflictError("Source has no revision to reindex")
            job = await self._job_for_revision(session, source.latest_revision_id, lock=True)
            if job.state in {"queued", "running"}:
                raise SourceConflictError("The latest revision is already queued or indexing")
            now = datetime.now(UTC)
            job.state = "queued"
            job.lease_owner = None
            job.lease_until = None
            job.error = None
            job.updated_at = now
            revision = await session.get(SourceRevision, source.latest_revision_id)
            if revision is None:
                raise SourceConflictError("Latest revision record is missing")
            revision.index_state = "queued"
            revision.error = None
            return job.id

    async def claim_job(self, owner: uuid.UUID) -> dict | None:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            statement = (
                select(Job)
                .where(
                    or_(
                        Job.state == "queued",
                        and_(
                            Job.state == "running",
                            or_(Job.lease_until.is_(None), Job.lease_until <= now),
                        ),
                    )
                )
                .order_by(Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = await session.scalar(statement)
            if job is None:
                return None
            job.state = "running"
            job.lease_owner = owner
            job.lease_until = now + _LEASE_DURATION
            job.attempts += 1
            job.error = None
            job.updated_at = now
            revision = await session.get(SourceRevision, job.revision_id)
            if revision is None:
                raise SourceConflictError("Index job references a missing revision")
            revision.index_state = "indexing"
            revision.error = None
            source = await session.get(SourceDocument, revision.source_id)
            if source is None:
                raise SourceConflictError("Index revision references a missing source")
            await session.flush()
            return {
                "job_id": str(job.id),
                "revision_id": str(revision.id),
                "source_id": str(source.id),
                "filename": revision.filename,
                "vault_path": revision.vault_path,
                "sha256": revision.sha256,
            }

    async def renew_lease(self, job_id: uuid.UUID, owner: uuid.UUID) -> bool:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            result = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.state == "running", Job.lease_owner == owner)
                .values(lease_until=now + _LEASE_DURATION, updated_at=now)
            )
            return result.rowcount == 1

    async def complete_job(
        self,
        job_id: uuid.UUID,
        owner: uuid.UUID,
        text_sha256: str,
        segments: list[dict],
    ) -> bool:
        if not _SHA256_PATTERN.fullmatch(text_sha256):
            raise ValueError("text_sha256 must be a lowercase hexadecimal SHA-256 digest")
        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            source_id = await session.scalar(
                select(SourceRevision.source_id)
                .join(Job, Job.revision_id == SourceRevision.id)
                .where(Job.id == job_id)
            )
            if source_id is None:
                return False
            source = await session.scalar(
                select(SourceDocument)
                .where(SourceDocument.id == source_id)
                .with_for_update()
            )
            if source is None:
                raise SourceConflictError("Index revision references a missing source")
            job = await session.scalar(
                select(Job)
                .where(
                    Job.id == job_id,
                    Job.state == "running",
                    Job.lease_owner == owner,
                    Job.lease_until > now,
                )
                .with_for_update()
            )
            if job is None:
                return False
            revision = await session.scalar(
                select(SourceRevision)
                .where(SourceRevision.id == job.revision_id)
                .with_for_update()
            )
            if revision is None:
                raise SourceConflictError("Index job references a missing revision")
            if revision.source_id != source.id:
                raise SourceConflictError("Index job revision changed source unexpectedly")
            # This timestamp belongs to the immutable content/parse snapshot.
            # Rebuilding the same index must not invalidate its evidence UUIDs
            # or change already published quotation pages. Job.updated_at still
            # records the completion time of each execution.
            if revision.indexed_at is None or revision.parsed_text_sha256 != text_sha256:
                revision.indexed_at = now
            revision.index_state = "ready"
            revision.parsed_text_sha256 = text_sha256
            revision.parsed_segments = segments
            revision.error = None
            job.state = "succeeded"
            job.lease_owner = None
            job.lease_until = None
            job.error = None
            job.updated_at = now
            if source.state == "active" and source.latest_revision_id == revision.id:
                source.current_revision_id = revision.id
            return True

    async def fail_job(self, job_id: uuid.UUID, owner: uuid.UUID, error: str) -> bool:
        now = datetime.now(UTC)
        bounded_error = (error or "Indexing failed")[:_MAX_ERROR_LENGTH]
        async with self.database.session_factory() as session, session.begin():
            job = await session.scalar(
                select(Job)
                .where(
                    Job.id == job_id,
                    Job.state == "running",
                    Job.lease_owner == owner,
                    Job.lease_until > now,
                )
                .with_for_update()
            )
            if job is None:
                return False
            revision = await session.scalar(
                select(SourceRevision)
                .where(SourceRevision.id == job.revision_id)
                .with_for_update()
            )
            if revision is None:
                raise SourceConflictError("Index job references a missing revision")
            revision.index_state = "failed"
            revision.error = bounded_error
            job.state = "failed"
            job.lease_owner = None
            job.lease_until = None
            job.error = bounded_error
            job.updated_at = now
            return True

    async def release_owner(self, owner: uuid.UUID) -> None:
        now = datetime.now(UTC)
        async with self.database.session_factory() as session, session.begin():
            owned_jobs = list(
                (
                    await session.scalars(
                        select(Job)
                        .where(Job.state == "running", Job.lease_owner == owner)
                        .with_for_update()
                    )
                ).all()
            )
            if not owned_jobs:
                return
            job_ids = [job.id for job in owned_jobs]
            revision_ids = [job.revision_id for job in owned_jobs]
            await session.execute(
                update(Job)
                .where(Job.id.in_(job_ids))
                .values(
                    state="queued",
                    lease_owner=None,
                    lease_until=None,
                    error=None,
                    updated_at=now,
                )
            )
            if revision_ids:
                await session.execute(
                    update(SourceRevision)
                    .where(SourceRevision.id.in_(revision_ids), SourceRevision.index_state == "indexing")
                    .values(index_state="queued", error=None)
                )

    async def _lock_hash(self, session: AsyncSession, sha256: str) -> None:
        lock_key = int.from_bytes(hashlib.sha256(sha256.encode("ascii")).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

    async def _find_active_hash(
        self, session: AsyncSession, sha256: str
    ) -> tuple[SourceDocument, SourceRevision, Job] | None:
        result = await session.execute(
            select(SourceDocument, SourceRevision, Job)
            .join(SourceRevision, SourceRevision.source_id == SourceDocument.id)
            .join(Job, and_(Job.revision_id == SourceRevision.id, Job.kind == "index"))
            .where(SourceDocument.state == "active", SourceRevision.sha256 == sha256)
            .order_by(SourceDocument.created_at, SourceDocument.id, SourceRevision.created_at)
            .limit(1)
        )
        row = result.first()
        return None if row is None else (row[0], row[1], row[2])

    async def _job_for_revision(
        self, session: AsyncSession, revision_id: uuid.UUID, lock: bool = False
    ) -> Job:
        statement = select(Job).where(Job.revision_id == revision_id, Job.kind == "index")
        if lock:
            statement = statement.with_for_update()
        job = await session.scalar(statement)
        if job is None:
            raise SourceConflictError("Index job record is missing for this revision")
        return job

    async def _source_snapshot(self, session: AsyncSession, source: SourceDocument) -> dict:
        latest = (
            await session.get(SourceRevision, source.latest_revision_id)
            if source.latest_revision_id is not None
            else None
        )
        current = (
            await session.get(SourceRevision, source.current_revision_id)
            if source.current_revision_id is not None
            else None
        )
        return self._source_snapshot_for_revisions(source, latest, current)

    def _source_snapshot_for_revisions(
        self,
        source: SourceDocument,
        latest: SourceRevision | None,
        current: SourceRevision | None,
    ) -> dict:
        latest_snapshot = self._revision_snapshot(latest) if latest is not None else None
        current_snapshot = self._revision_snapshot(current) if current is not None else None
        return {
            "id": str(source.id),
            "source_id": str(source.id),
            "filename": source.filename,
            "state": source.state,
            "latest_revision_id": str(source.latest_revision_id)
            if source.latest_revision_id is not None
            else None,
            "current_revision_id": str(source.current_revision_id)
            if source.current_revision_id is not None
            else None,
            "revision_status": latest.index_state if latest is not None else None,
            "sha256": latest.sha256 if latest is not None else None,
            "vault_path": latest.vault_path if latest is not None else None,
            "error": latest.error if latest is not None else None,
            "latest_revision": latest_snapshot,
            "current_revision": current_snapshot,
            "created_at": self._iso(source.created_at),
        }

    @classmethod
    def _revision_snapshot(cls, revision: SourceRevision) -> dict:
        return {
            "id": str(revision.id),
            "revision_id": str(revision.id),
            "source_id": str(revision.source_id),
            "filename": revision.filename,
            "sha256": revision.sha256,
            "vault_path": revision.vault_path,
            "media_type": revision.media_type,
            "state": revision.index_state,
            "index_state": revision.index_state,
            "parsed_text_sha256": revision.parsed_text_sha256,
            "error": revision.error,
            "created_at": cls._iso(revision.created_at),
            "indexed_at": cls._iso(revision.indexed_at),
        }

    @classmethod
    def _job_snapshot(cls, job: Job) -> dict:
        return {
            "id": str(job.id),
            "job_id": str(job.id),
            "kind": job.kind,
            "revision_id": str(job.revision_id),
            "state": job.state,
            "attempts": job.attempts,
            "lease_owner": str(job.lease_owner) if job.lease_owner is not None else None,
            "lease_until": cls._iso(job.lease_until),
            "error": job.error,
            "created_at": cls._iso(job.created_at),
            "updated_at": cls._iso(job.updated_at),
        }

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
