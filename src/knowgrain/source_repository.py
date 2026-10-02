from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Sequence

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from knowgrain.database import ApplicationDatabase
from knowgrain.models import (
    CoreMaintenanceJob,
    Job,
    SourceDocument,
    SourceFileOperation,
    SourceRevision,
)


PersistSource = Callable[[uuid.UUID, uuid.UUID], Awaitable[str]]
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEASE_DURATION = timedelta(seconds=90)
_MAX_ERROR_LENGTH = 4000
_MAX_CLEANUP_CHUNKS = 10_000
_MAINTENANCE_CLEANED_ERROR = "已清理索引，恢复后将重建"


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

    async def list_sources(
        self, *, limit: int = 100, offset: int = 0, state: str = "all"
    ) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be zero or greater")
        if state not in {"all", "active", "deleted"}:
            raise ValueError("state must be 'all', 'active', or 'deleted'")

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
            if state != "all":
                statement = statement.where(SourceDocument.state == state)
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

    async def soft_delete_source(
        self,
        source_id: uuid.UUID,
        *,
        expected_lifecycle_version: int,
        expected_latest_revision_id: uuid.UUID | None,
    ) -> dict:
        self._validate_expected_lifecycle_version(expected_lifecycle_version)
        async with self.database.session_factory() as session, session.begin():
            source = await session.scalar(
                select(SourceDocument)
                .where(SourceDocument.id == source_id)
                .with_for_update()
            )
            if source is None:
                raise SourceNotFoundError(f"Source {source_id} does not exist")

            if (
                source.state == "deleted"
                and source.lifecycle_version == expected_lifecycle_version + 1
                and source.latest_revision_id == expected_latest_revision_id
            ):
                return await self._source_snapshot(session, source)
            if (
                source.state != "active"
                or source.lifecycle_version != expected_lifecycle_version
                or source.latest_revision_id != expected_latest_revision_id
            ):
                raise SourceConflictError("Source changed; refresh before deleting")

            jobs = list(
                (
                    await session.scalars(
                        select(Job)
                        .join(SourceRevision, SourceRevision.id == Job.revision_id)
                        .where(SourceRevision.source_id == source.id)
                        .order_by(Job.id)
                        .with_for_update(of=Job)
                    )
                ).all()
            )
            maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
            file_operations = await self._lock_source_file_operations(session, source.id)
            if any(
                operation.kind == "restore" and operation.state in {"queued", "running"}
                for operation in file_operations
            ):
                raise SourceConflictError("A restore operation is already pending")
            revisions = list(
                (
                    await session.scalars(
                        select(SourceRevision)
                        .where(SourceRevision.source_id == source.id)
                        .order_by(SourceRevision.id)
                        .with_for_update()
                    )
                ).all()
            )
            now = await self._database_now(session)
            for job in jobs:
                if job.state in {"queued", "running"}:
                    job.state = "failed"
                    job.lease_owner = None
                    job.lease_until = None
                    job.error = "Source deleted"
                    job.updated_at = now
            lifecycle_version = source.lifecycle_version + 1
            existing_revision_ids = {
                maintenance.revision_id
                for maintenance in maintenance_jobs
                if maintenance.lifecycle_version == lifecycle_version
            }
            for revision in revisions:
                if revision.id not in existing_revision_ids:
                    session.add(
                        CoreMaintenanceJob(
                            id=uuid.uuid4(),
                            source_id=source.id,
                            revision_id=revision.id,
                            lifecycle_version=lifecycle_version,
                            state="queued",
                            cleanup_chunk_ids=self._merge_cleanup_manifests(
                                revision.id, jobs, maintenance_jobs
                            ),
                            attempts=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                if revision.index_state in {"queued", "indexing"}:
                    revision.index_state = "failed"
                    revision.error = "Source deleted"
            source.state = "deleted"
            source.lifecycle_version = lifecycle_version
            await session.flush()
            return await self._source_snapshot(session, source)

    async def restore_source(
        self,
        source_id: uuid.UUID,
        *,
        expected_lifecycle_version: int,
        expected_latest_revision_id: uuid.UUID | None,
        verified_current_revision_id: uuid.UUID | None,
    ) -> dict:
        self._validate_expected_lifecycle_version(expected_lifecycle_version)
        async with self.database.session_factory() as session, session.begin():
            source = await session.scalar(
                select(SourceDocument)
                .where(SourceDocument.id == source_id)
                .with_for_update()
            )
            if source is None:
                raise SourceNotFoundError(f"Source {source_id} does not exist")

            index_jobs = await self._lock_source_index_jobs(session, source.id)
            maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
            file_operations = await self._lock_source_file_operations(session, source.id)
            revisions = await self._lock_source_revisions(session, source.id)

            if (
                source.state == "active"
                and source.lifecycle_version == expected_lifecycle_version + 1
                and source.latest_revision_id == expected_latest_revision_id
                and (
                    source.current_revision_id == verified_current_revision_id
                    or source.current_revision_id is None
                )
            ):
                return await self._source_snapshot(session, source)
            if any(
                operation.lifecycle_version == expected_lifecycle_version
                and operation.state != "cancelled"
                for operation in file_operations
            ):
                raise SourceConflictError("Source file operations must use the file journal")
            if (
                source.state != "deleted"
                or source.lifecycle_version != expected_lifecycle_version
                or source.latest_revision_id != expected_latest_revision_id
                or source.current_revision_id != verified_current_revision_id
            ):
                raise SourceConflictError("Source changed; refresh before restoring")

            if any(job.state == "running" for job in maintenance_jobs):
                raise SourceConflictError("Core cleanup is still running; retry restore later")

            now = await self._database_now(session)
            return await self._activate_restored_source(
                session,
                source,
                index_jobs=index_jobs,
                maintenance_jobs=maintenance_jobs,
                file_operations=file_operations,
                revisions=revisions,
                expected_latest_revision_id=expected_latest_revision_id,
                verified_current_revision_id=verified_current_revision_id,
                now=now,
                restore_operation=None,
            )

    async def _activate_restored_source(
        self,
        session: AsyncSession,
        source: SourceDocument,
        *,
        index_jobs: Sequence[Job],
        maintenance_jobs: Sequence[CoreMaintenanceJob],
        file_operations: Sequence[SourceFileOperation],
        revisions: Sequence[SourceRevision],
        expected_latest_revision_id: uuid.UUID | None,
        verified_current_revision_id: uuid.UUID | None,
        now: datetime,
        restore_operation: SourceFileOperation | None,
    ) -> dict:
        """Activate a deleted source after its exact archive restore has completed.

        The file-journal path passes the succeeded current-cycle restore row. The
        legacy repository path is retained only for sources with no live file
        journal, which keeps pre-M5 repository-only tests and installations
        without archived source files compatible.
        """
        if restore_operation is None:
            if any(
                operation.lifecycle_version == source.lifecycle_version
                and operation.state != "cancelled"
                for operation in file_operations
            ):
                raise SourceConflictError("Source file operations must use the file journal")
        elif (
            restore_operation not in file_operations
            or restore_operation.kind != "restore"
            or restore_operation.state != "succeeded"
            or restore_operation.lifecycle_version != source.lifecycle_version
            or restore_operation.expected_latest_revision_id != expected_latest_revision_id
            or restore_operation.verified_current_revision_id != verified_current_revision_id
        ):
            raise SourceConflictError("Restore operation does not match this source lifecycle")

        if (
            source.state != "deleted"
            or source.latest_revision_id != expected_latest_revision_id
            or source.current_revision_id != verified_current_revision_id
        ):
            raise SourceConflictError("Source changed; refresh before restoring")

        attempted_cleanup = any(
            job.lifecycle_version == source.lifecycle_version and job.attempts > 0
            for job in maintenance_jobs
        )
        for maintenance in maintenance_jobs:
            if maintenance.lifecycle_version != source.lifecycle_version:
                continue
            if maintenance.state == "queued" or (
                maintenance.state == "failed" and maintenance.attempts == 0
            ):
                maintenance.state = "cancelled"
                maintenance.lease_owner = None
                maintenance.lease_until = None
                maintenance.error = None
                maintenance.updated_at = now

        if attempted_cleanup and source.latest_revision_id is not None:
            latest_job = next(
                (
                    job
                    for job in index_jobs
                    if job.revision_id == source.latest_revision_id
                ),
                None,
            )
            latest_revision = next(
                (
                    revision
                    for revision in revisions
                    if revision.id == source.latest_revision_id
                ),
                None,
            )
            if latest_job is None or latest_revision is None:
                raise SourceConflictError("Latest revision indexing record is missing")
            latest_job.state = "queued"
            latest_job.force_rebuild = True
            latest_job.cleanup_chunk_ids = self._merge_cleanup_manifests(
                latest_revision.id, index_jobs, maintenance_jobs
            )
            latest_job.lease_owner = None
            latest_job.lease_until = None
            latest_job.error = None
            latest_job.updated_at = now
            latest_revision.index_state = "queued"
            latest_revision.error = None
            source.current_revision_id = None

        source.state = "active"
        source.lifecycle_version += 1
        await session.flush()
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
            jobs = await self._lock_source_index_jobs(session, source.id)
            maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
            await self._lock_source_file_operations(session, source.id)
            revisions = await self._lock_source_revisions(session, source.id)
            job = next(
                (candidate for candidate in jobs if candidate.revision_id == source.latest_revision_id),
                None,
            )
            if job is None:
                raise SourceConflictError("Index job record is missing for this revision")
            if job.state in {"queued", "running"}:
                raise SourceConflictError("The latest revision is already queued or indexing")
            revision = next(
                (candidate for candidate in revisions if candidate.id == source.latest_revision_id),
                None,
            )
            if revision is None:
                raise SourceConflictError("Latest revision record is missing")
            now = await self._database_now(session)
            job.state = "queued"
            job.lease_owner = None
            job.lease_until = None
            job.error = None
            job.force_rebuild = True
            job.cleanup_chunk_ids = self._merge_cleanup_manifests(
                revision.id, [job], maintenance_jobs
            )
            job.updated_at = now
            revision.index_state = "queued"
            revision.error = None
            return job.id

    async def claim_job(self, owner: uuid.UUID) -> dict | None:
        async with self.database.session_factory() as session, session.begin():
            statement = (
                select(SourceDocument, Job.id)
                .join(SourceRevision, SourceRevision.source_id == SourceDocument.id)
                .join(Job, Job.revision_id == SourceRevision.id)
                .where(
                    SourceDocument.state == "active",
                    or_(
                        Job.state == "queued",
                        and_(
                            Job.state == "running",
                            or_(Job.lease_until.is_(None), Job.lease_until <= func.clock_timestamp()),
                        ),
                    ),
                )
                .order_by(Job.created_at, Job.id, SourceDocument.id)
                .with_for_update(of=SourceDocument, skip_locked=True)
                .limit(1)
            )
            candidate = (await session.execute(statement)).first()
            if candidate is None:
                return None
            source, job_id = candidate
            jobs = await self._lock_source_index_jobs(session, source.id)
            _maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
            _file_operations = await self._lock_source_file_operations(session, source.id)
            revisions = await self._lock_source_revisions(session, source.id)
            job = next((candidate for candidate in jobs if candidate.id == job_id), None)
            if job is None:
                return None
            revision = next(
                (candidate for candidate in revisions if candidate.id == job.revision_id), None
            )
            if revision is None:
                raise SourceConflictError("Index job references a missing revision")
            now = await self._database_now(session)
            if source.state != "active" or not self._is_claimable(job, now):
                return None
            job.state = "running"
            job.lease_owner = owner
            job.lease_until = now + _LEASE_DURATION
            job.attempts += 1
            job.error = None
            job.updated_at = now
            revision.index_state = "indexing"
            revision.error = None
            await session.flush()
            return {
                "job_id": str(job.id),
                "revision_id": str(revision.id),
                "source_id": str(source.id),
                "filename": revision.filename,
                "vault_path": revision.vault_path,
                "sha256": revision.sha256,
                "force_rebuild": job.force_rebuild,
                "cleanup_chunk_ids": list(job.cleanup_chunk_ids)
                if job.cleanup_chunk_ids is not None
                else None,
            }

    async def record_index_cleanup_chunks(
        self,
        job_id: uuid.UUID,
        owner: uuid.UUID,
        chunk_ids: Sequence[str],
    ) -> bool:
        manifest = self._normalize_cleanup_chunks(chunk_ids)
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_job_context(session, job_id)
            if context is None:
                return False
            source, job, _revision = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, job, owner, now) or not job.force_rebuild:
                return False
            job.cleanup_chunk_ids = list(manifest)
            job.updated_at = now
            return True

    async def claim_maintenance(self, owner: uuid.UUID) -> dict | None:
        async with self.database.session_factory() as session, session.begin():
            statement = (
                select(SourceDocument, SourceRevision.id, CoreMaintenanceJob.id, Job.id)
                .join(SourceRevision, SourceRevision.source_id == SourceDocument.id)
                .join(Job, Job.revision_id == SourceRevision.id)
                .join(
                    CoreMaintenanceJob,
                    and_(
                        CoreMaintenanceJob.revision_id == SourceRevision.id,
                        CoreMaintenanceJob.source_id == SourceDocument.id,
                    ),
                )
                .where(
                    SourceDocument.state == "deleted",
                    CoreMaintenanceJob.lifecycle_version == SourceDocument.lifecycle_version,
                    or_(
                        CoreMaintenanceJob.state == "queued",
                        and_(
                            CoreMaintenanceJob.state == "running",
                            or_(
                                CoreMaintenanceJob.lease_until.is_(None),
                                CoreMaintenanceJob.lease_until <= func.clock_timestamp(),
                            ),
                        ),
                    ),
                )
                .order_by(CoreMaintenanceJob.created_at, CoreMaintenanceJob.id)
                .with_for_update(of=SourceDocument, skip_locked=True)
                .limit(1)
            )
            candidate = (await session.execute(statement)).first()
            if candidate is None:
                return None
            source, revision_id, maintenance_id, index_job_id = candidate
            index_jobs = await self._lock_source_index_jobs(session, source.id)
            maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
            file_operations = await self._lock_source_file_operations(session, source.id)
            revisions = await self._lock_source_revisions(session, source.id)
            index_job = next((job for job in index_jobs if job.id == index_job_id), None)
            maintenance = next(
                (job for job in maintenance_jobs if job.id == maintenance_id), None
            )
            if maintenance is None:
                return None
            revision = next((item for item in revisions if item.id == revision_id), None)
            if revision is None:
                raise SourceConflictError("Maintenance job references a missing revision")
            now = await self._database_now(session)
            if (
                source.state != "deleted"
                or maintenance.lifecycle_version != source.lifecycle_version
                or not self._is_claimable(maintenance, now)
            ):
                return None
            if index_job is None:
                return None
            if any(
                operation.kind == "restore"
                and operation.lifecycle_version == source.lifecycle_version
                and operation.state != "cancelled"
                for operation in file_operations
            ):
                return None
            if revision.source_id != source.id or index_job.revision_id != revision.id:
                raise SourceConflictError("Maintenance job revision changed source unexpectedly")
            maintenance.state = "running"
            maintenance.lease_owner = owner
            maintenance.lease_until = now + _LEASE_DURATION
            maintenance.attempts += 1
            maintenance.error = None
            maintenance.updated_at = now
            await session.flush()
            return {
                "job_id": str(maintenance.id),
                "source_id": str(source.id),
                "revision_id": str(revision.id),
                "lifecycle_version": maintenance.lifecycle_version,
                "cleanup_chunk_ids": list(maintenance.cleanup_chunk_ids)
                if maintenance.cleanup_chunk_ids is not None
                else None,
            }

    async def renew_maintenance_lease(self, job_id: uuid.UUID, owner: uuid.UUID) -> bool:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_maintenance_context(session, job_id)
            if context is None:
                return False
            source, maintenance, _revision, _file_operations = context
            now = await self._database_now(session)
            if not self._has_live_maintenance_lease(source, maintenance, owner, now):
                return False
            maintenance.lease_until = now + _LEASE_DURATION
            maintenance.updated_at = now
            return True

    async def record_maintenance_chunks(
        self,
        job_id: uuid.UUID,
        owner: uuid.UUID,
        chunk_ids: Sequence[str],
    ) -> bool:
        manifest = self._normalize_cleanup_chunks(chunk_ids)
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_maintenance_context(session, job_id)
            if context is None:
                return False
            source, maintenance, _revision, _file_operations = context
            now = await self._database_now(session)
            if not self._has_live_maintenance_lease(source, maintenance, owner, now):
                return False
            maintenance.cleanup_chunk_ids = list(manifest)
            maintenance.updated_at = now
            return True

    async def complete_maintenance(self, job_id: uuid.UUID, owner: uuid.UUID) -> bool:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_maintenance_context(session, job_id)
            if context is None:
                return False
            source, maintenance, revision, _file_operations = context
            now = await self._database_now(session)
            if not self._has_live_maintenance_lease(source, maintenance, owner, now):
                return False
            maintenance.state = "succeeded"
            maintenance.lease_owner = None
            maintenance.lease_until = None
            maintenance.error = None
            maintenance.updated_at = now
            revision.index_state = "failed"
            revision.error = _MAINTENANCE_CLEANED_ERROR
            from knowgrain.source_file_repository import enqueue_archive_for_source

            await enqueue_archive_for_source(session, source)
            return True

    async def fail_maintenance(
        self, job_id: uuid.UUID, owner: uuid.UUID, safe_error: str
    ) -> bool:
        bounded_error = (safe_error or "Core cleanup failed")[:_MAX_ERROR_LENGTH]
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_maintenance_context(session, job_id)
            if context is None:
                return False
            source, maintenance, _revision, _file_operations = context
            now = await self._database_now(session)
            if not self._has_live_maintenance_lease(source, maintenance, owner, now):
                return False
            maintenance.state = "failed"
            maintenance.lease_owner = None
            maintenance.lease_until = None
            maintenance.error = bounded_error
            maintenance.updated_at = now
            return True

    async def release_maintenance_owner(self, owner: uuid.UUID) -> None:
        async with self.database.session_factory() as session, session.begin():
            source_ids = list(
                (
                    await session.scalars(
                        select(SourceDocument.id)
                        .join(
                            CoreMaintenanceJob,
                            CoreMaintenanceJob.source_id == SourceDocument.id,
                        )
                        .where(
                            CoreMaintenanceJob.state == "running",
                            CoreMaintenanceJob.lease_owner == owner,
                        )
                        .distinct()
                        .order_by(SourceDocument.id)
                    )
                ).all()
            )
            for source_id in source_ids:
                source = await session.scalar(
                    select(SourceDocument)
                    .where(SourceDocument.id == source_id)
                    .with_for_update()
                )
                if source is None:
                    continue
                await self._lock_source_index_jobs(session, source.id)
                maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
                await self._lock_source_file_operations(session, source.id)
                await self._lock_source_revisions(session, source.id)
                now = await self._database_now(session)
                for maintenance in maintenance_jobs:
                    if maintenance.state != "running" or maintenance.lease_owner != owner:
                        continue
                    current_cycle = (
                        source.state == "deleted"
                        and source.lifecycle_version == maintenance.lifecycle_version
                    )
                    maintenance.state = "failed" if current_cycle else "cancelled"
                    maintenance.lease_owner = None
                    maintenance.lease_until = None
                    maintenance.error = (
                        "Core cleanup stopped before completion" if current_cycle else None
                    )
                    maintenance.updated_at = now

    async def retry_maintenance(self, job_id: uuid.UUID) -> dict:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_maintenance_context(session, job_id)
            if context is None:
                raise SourceNotFoundError(f"Maintenance job {job_id} does not exist")
            source, maintenance, _revision, file_operations = context
            if (
                source.state != "deleted"
                or source.lifecycle_version != maintenance.lifecycle_version
            ):
                raise SourceConflictError("Maintenance job is not for the current deleted cycle")
            if any(
                operation.kind == "restore"
                and operation.lifecycle_version == source.lifecycle_version
                and operation.state != "cancelled"
                for operation in file_operations
            ):
                raise SourceConflictError("A restore operation blocks Core cleanup")
            if maintenance.state in {"running", "succeeded", "cancelled"}:
                raise SourceConflictError("Maintenance job cannot be retried in its current state")
            now = await self._database_now(session)
            if maintenance.state == "failed":
                maintenance.state = "queued"
                maintenance.lease_owner = None
                maintenance.lease_until = None
                maintenance.error = None
                maintenance.updated_at = now
            return self._maintenance_snapshot(maintenance)

    async def list_maintenance(
        self, source_id: uuid.UUID, *, limit: int = 100
    ) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self.database.session_factory() as session:
            rows = await session.scalars(
                select(CoreMaintenanceJob)
                .where(CoreMaintenanceJob.source_id == source_id)
                .order_by(CoreMaintenanceJob.created_at.desc(), CoreMaintenanceJob.id)
                .limit(limit)
            )
            return [self._maintenance_snapshot(job) for job in rows]

    async def renew_lease(self, job_id: uuid.UUID, owner: uuid.UUID) -> bool:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_job_context(session, job_id)
            if context is None:
                return False
            source, job, _revision = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, job, owner, now):
                return False
            job.lease_until = now + _LEASE_DURATION
            job.updated_at = now
            return True

    async def complete_job(
        self,
        job_id: uuid.UUID,
        owner: uuid.UUID,
        text_sha256: str,
        segments: list[dict],
    ) -> bool:
        if not _SHA256_PATTERN.fullmatch(text_sha256):
            raise ValueError("text_sha256 must be a lowercase hexadecimal SHA-256 digest")
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_job_context(session, job_id)
            if context is None:
                return False
            source, job, revision = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, job, owner, now):
                return False
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
            job.force_rebuild = False
            job.updated_at = now
            if source.state == "active" and source.latest_revision_id == revision.id:
                source.current_revision_id = revision.id
            return True

    async def fail_job(self, job_id: uuid.UUID, owner: uuid.UUID, error: str) -> bool:
        bounded_error = (error or "Indexing failed")[:_MAX_ERROR_LENGTH]
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_job_context(session, job_id)
            if context is None:
                return False
            source, job, revision = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, job, owner, now):
                return False
            revision.index_state = "failed"
            revision.error = bounded_error
            job.state = "failed"
            job.lease_owner = None
            job.lease_until = None
            job.error = bounded_error
            job.updated_at = now
            return True

    async def release_owner(self, owner: uuid.UUID) -> None:
        async with self.database.session_factory() as session, session.begin():
            source_ids = list(
                (
                    await session.scalars(
                        select(SourceDocument.id)
                        .join(SourceRevision, SourceRevision.source_id == SourceDocument.id)
                        .join(Job, Job.revision_id == SourceRevision.id)
                        .where(Job.state == "running", Job.lease_owner == owner)
                        .distinct()
                        .order_by(SourceDocument.id)
                    )
                ).all()
            )
            for source_id in source_ids:
                source = await session.scalar(
                    select(SourceDocument)
                    .where(SourceDocument.id == source_id)
                    .with_for_update()
                )
                if source is None:
                    continue
                all_jobs = await self._lock_source_index_jobs(session, source.id)
                await self._lock_source_maintenance_jobs(session, source.id)
                await self._lock_source_file_operations(session, source.id)
                revisions = await self._lock_source_revisions(session, source.id)
                jobs = [
                    job
                    for job in all_jobs
                    if job.state == "running" and job.lease_owner == owner
                ]
                now = await self._database_now(session)
                deleted = source.state != "active"
                revision_by_id = {revision.id: revision for revision in revisions}
                for job in jobs:
                    job.state = "failed" if deleted else "queued"
                    job.lease_owner = None
                    job.lease_until = None
                    job.error = "Source deleted" if deleted else None
                    job.updated_at = now
                    revision = revision_by_id.get(job.revision_id)
                    if revision is not None and revision.index_state == "indexing":
                        revision.index_state = "failed" if deleted else "queued"
                        revision.error = "Source deleted" if deleted else None

    async def _lock_source_index_jobs(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> list[Job]:
        return list(
            (
                await session.scalars(
                    select(Job)
                    .join(SourceRevision, SourceRevision.id == Job.revision_id)
                    .where(SourceRevision.source_id == source_id)
                    .order_by(Job.id)
                    .with_for_update(of=Job)
                )
            ).all()
        )

    async def _lock_source_maintenance_jobs(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> list[CoreMaintenanceJob]:
        return list(
            (
                await session.scalars(
                    select(CoreMaintenanceJob)
                    .where(CoreMaintenanceJob.source_id == source_id)
                    .order_by(CoreMaintenanceJob.id)
                    .with_for_update()
                )
            ).all()
        )

    async def _lock_source_file_operations(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> list[SourceFileOperation]:
        return list(
            (
                await session.scalars(
                    select(SourceFileOperation)
                    .where(SourceFileOperation.source_id == source_id)
                    .order_by(SourceFileOperation.id)
                    .with_for_update()
                )
            ).all()
        )

    async def _lock_revision_maintenance_jobs(
        self, session: AsyncSession, revision_id: uuid.UUID
    ) -> list[CoreMaintenanceJob]:
        return list(
            (
                await session.scalars(
                    select(CoreMaintenanceJob)
                    .where(CoreMaintenanceJob.revision_id == revision_id)
                    .order_by(CoreMaintenanceJob.id)
                    .with_for_update()
                )
            ).all()
        )

    async def _lock_source_revisions(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> list[SourceRevision]:
        return list(
            (
                await session.scalars(
                    select(SourceRevision)
                    .where(SourceRevision.source_id == source_id)
                    .order_by(SourceRevision.id)
                    .with_for_update()
                )
            ).all()
        )

    async def _lock_maintenance_context(
        self, session: AsyncSession, job_id: uuid.UUID
    ) -> tuple[
        SourceDocument,
        CoreMaintenanceJob,
        SourceRevision,
        list[SourceFileOperation],
    ] | None:
        identity = await session.execute(
            select(CoreMaintenanceJob.source_id, CoreMaintenanceJob.revision_id)
            .where(CoreMaintenanceJob.id == job_id)
        )
        row = identity.first()
        if row is None:
            return None
        source_id, revision_id = row
        source = await session.scalar(
            select(SourceDocument)
            .where(SourceDocument.id == source_id)
            .with_for_update()
        )
        if source is None:
            raise SourceConflictError("Maintenance job references a missing source")
        index_jobs = await self._lock_source_index_jobs(session, source.id)
        maintenance_jobs = await self._lock_source_maintenance_jobs(session, source.id)
        file_operations = await self._lock_source_file_operations(session, source.id)
        revisions = await self._lock_source_revisions(session, source.id)
        index_job = next((job for job in index_jobs if job.revision_id == revision_id), None)
        maintenance = next((job for job in maintenance_jobs if job.id == job_id), None)
        revision = next((item for item in revisions if item.id == revision_id), None)
        if maintenance is None:
            return None
        if index_job is None:
            raise SourceConflictError("Maintenance job revision has no index job")
        if revision is None:
            raise SourceConflictError("Maintenance job references a missing revision")
        if (
            maintenance.revision_id != revision.id
            or maintenance.source_id != source.id
            or revision.source_id != source.id
            or index_job.revision_id != revision.id
        ):
            raise SourceConflictError("Maintenance job revision changed source unexpectedly")
        return source, maintenance, revision, file_operations

    @staticmethod
    def _normalize_cleanup_chunks(chunk_ids: Sequence[str]) -> tuple[str, ...]:
        if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, Sequence):
            raise ValueError("chunk_ids must be a sequence of text chunk IDs")
        if len(chunk_ids) > _MAX_CLEANUP_CHUNKS:
            raise ValueError("cleanup manifest exceeds the 10000 chunk limit")
        unique: dict[str, None] = {}
        for chunk_id in chunk_ids:
            if not isinstance(chunk_id, str) or not 1 <= len(chunk_id) <= 512:
                raise ValueError("each chunk ID must contain between 1 and 512 characters")
            if any(unicodedata.category(char) == "Cc" for char in chunk_id):
                raise ValueError("chunk IDs must not contain control characters")
            unique.setdefault(chunk_id, None)
        return tuple(unique)

    @classmethod
    def _merge_cleanup_manifests(
        cls, revision_id: uuid.UUID, index_jobs: Sequence[Job],
        maintenance_jobs: Sequence[CoreMaintenanceJob],
    ) -> list[str] | None:
        known: set[str] = set()
        found = False
        for job in (*index_jobs, *maintenance_jobs):
            if job.revision_id != revision_id or job.cleanup_chunk_ids is None:
                continue
            found = True
            try:
                known.update(cls._normalize_cleanup_chunks(job.cleanup_chunk_ids))
            except ValueError:
                raise SourceConflictError("Stored cleanup manifest is invalid") from None
            if len(known) > _MAX_CLEANUP_CHUNKS:
                raise SourceConflictError("Stored cleanup manifest exceeds the supported limit")
        return sorted(known) if found else None

    async def _lock_hash(self, session: AsyncSession, sha256: str) -> None:
        lock_key = int.from_bytes(hashlib.sha256(sha256.encode("ascii")).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

    async def _lock_job_context(
        self, session: AsyncSession, job_id: uuid.UUID
    ) -> tuple[SourceDocument, Job, SourceRevision] | None:
        identity = await session.execute(
            select(SourceRevision.source_id, Job.revision_id)
            .join(Job, Job.revision_id == SourceRevision.id)
            .where(Job.id == job_id)
        )
        row = identity.first()
        if row is None:
            return None
        source_id, revision_id = row
        source = await session.scalar(
            select(SourceDocument)
            .where(SourceDocument.id == source_id)
            .with_for_update()
        )
        if source is None:
            raise SourceConflictError("Index revision references a missing source")
        index_jobs = await self._lock_source_index_jobs(session, source.id)
        await self._lock_source_maintenance_jobs(session, source.id)
        await self._lock_source_file_operations(session, source.id)
        revisions = await self._lock_source_revisions(session, source.id)
        job = next((candidate for candidate in index_jobs if candidate.id == job_id), None)
        if job is None:
            return None
        revision = next((candidate for candidate in revisions if candidate.id == revision_id), None)
        if revision is None:
            raise SourceConflictError("Index job references a missing revision")
        if job.revision_id != revision.id or revision.source_id != source.id:
            raise SourceConflictError("Index job revision changed source unexpectedly")
        return source, job, revision

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        value = await session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("PostgreSQL did not return the database clock")
        return value

    @staticmethod
    def _validate_expected_lifecycle_version(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("expected_lifecycle_version must be a nonnegative integer")

    @staticmethod
    def _is_claimable(job: Job, now: datetime) -> bool:
        return job.state == "queued" or (
            job.state == "running" and (job.lease_until is None or job.lease_until <= now)
        )

    @staticmethod
    def _has_live_lease(
        source: SourceDocument, job: Job, owner: uuid.UUID, now: datetime
    ) -> bool:
        return (
            source.state == "active"
            and job.state == "running"
            and job.lease_owner == owner
            and job.lease_until is not None
            and job.lease_until > now
        )

    @staticmethod
    def _has_live_maintenance_lease(
        source: SourceDocument,
        job: CoreMaintenanceJob,
        owner: uuid.UUID,
        now: datetime,
    ) -> bool:
        return (
            source.state == "deleted"
            and source.lifecycle_version == job.lifecycle_version
            and job.state == "running"
            and job.lease_owner == owner
            and job.lease_until is not None
            and job.lease_until > now
        )

    @classmethod
    def _maintenance_snapshot(cls, job: CoreMaintenanceJob) -> dict:
        return {
            "job_id": str(job.id),
            "source_id": str(job.source_id),
            "revision_id": str(job.revision_id),
            "lifecycle_version": job.lifecycle_version,
            "state": job.state,
            "attempts": job.attempts,
            "error": job.error,
            "created_at": cls._iso(job.created_at),
            "updated_at": cls._iso(job.updated_at),
            "lease_until": cls._iso(job.lease_until),
        }

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
            "lifecycle_version": source.lifecycle_version,
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
