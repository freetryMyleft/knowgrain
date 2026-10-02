"""Durable source archive and restore operation journal."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowgrain.database import ApplicationDatabase
from knowgrain.models import (
    CoreMaintenanceJob,
    Job,
    SourceDocument,
    SourceFileOperation,
    SourceRevision,
)
from knowgrain.source_repository import SourceConflictError, SourceNotFoundError

if TYPE_CHECKING:
    from knowgrain.source_repository import SourceRepository


_LEASE_DURATION = timedelta(seconds=90)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf", ".docx"}
_INVALID_MANIFEST_ERROR = "Source archive manifest is invalid or exceeds the supported limit"
_ARCHIVE_ERROR = "Source archive operation could not be completed"


def _raw_manifest(revisions: Sequence[SourceRevision]) -> list[dict[str, str]]:
    return [
        {
            "revision_id": str(revision.id),
            "vault_path": revision.vault_path,
            "sha256": revision.sha256,
        }
        for revision in sorted(revisions, key=lambda item: item.id.int)
    ]


def _manifest_error(manifest: Any, source_id: uuid.UUID) -> str | None:
    if isinstance(manifest, (str, bytes)) or not isinstance(manifest, list):
        return _INVALID_MANIFEST_ERROR
    if not 1 <= len(manifest) <= 10_000:
        return _INVALID_MANIFEST_ERROR
    seen: set[uuid.UUID] = set()
    previous: uuid.UUID | None = None
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {"revision_id", "vault_path", "sha256"}:
            return _INVALID_MANIFEST_ERROR
        revision_value = item.get("revision_id")
        try:
            revision_id = uuid.UUID(revision_value) if isinstance(revision_value, str) else None
        except (ValueError, AttributeError):
            return _INVALID_MANIFEST_ERROR
        if revision_id is None or str(revision_id) != revision_value or revision_id in seen:
            return _INVALID_MANIFEST_ERROR
        if previous is not None and revision_id.int <= previous.int:
            return _INVALID_MANIFEST_ERROR
        previous = revision_id
        seen.add(revision_id)

        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            return _INVALID_MANIFEST_ERROR
        path = item.get("vault_path")
        if not isinstance(path, str):
            return _INVALID_MANIFEST_ERROR
        prefix = f"Sources/Files/{source_id}/{revision_id}"
        if not path.startswith(prefix):
            return _INVALID_MANIFEST_ERROR
        suffix = path[len(prefix) :]
        if suffix not in _SUPPORTED_SUFFIXES or path != f"{prefix}{suffix}":
            return _INVALID_MANIFEST_ERROR
    return None


def _manifest_matches(
    manifest: Any, source_id: uuid.UUID, revisions: Sequence[SourceRevision]
) -> bool:
    return _manifest_error(manifest, source_id) is None and manifest == _raw_manifest(revisions)


async def _lock_source_index_jobs(session: AsyncSession, source_id: uuid.UUID) -> list[Job]:
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
    session: AsyncSession, source_id: uuid.UUID
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


async def lock_source_file_operations(
    session: AsyncSession, source_id: uuid.UUID
) -> list[SourceFileOperation]:
    """Lock a source's file journal after Core maintenance and before revisions."""
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


async def _lock_source_revisions(
    session: AsyncSession, source_id: uuid.UUID
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


async def _locked_source_rows(
    session: AsyncSession, source_id: uuid.UUID
) -> tuple[list[Job], list[CoreMaintenanceJob], list[SourceFileOperation], list[SourceRevision]]:
    # Every same-source mutation uses this order after locking SourceDocument.
    jobs = await _lock_source_index_jobs(session, source_id)
    maintenance = await _lock_source_maintenance_jobs(session, source_id)
    file_operations = await lock_source_file_operations(session, source_id)
    revisions = await _lock_source_revisions(session, source_id)
    return jobs, maintenance, file_operations, revisions


async def enqueue_archive_for_source(
    session: AsyncSession, source: SourceDocument
) -> SourceFileOperation | None:
    """Create or preserve the archive intent once every current cleanup succeeded.

    The caller must already hold the SourceDocument row lock. Manifest validation
    failures are journaled as failed operations so the acknowledged Core cleanup
    remains committed and the archive intent remains visible for repair.
    """
    _jobs, maintenance, file_operations, revisions = await _locked_source_rows(
        session, source.id
    )
    return _enqueue_archive_locked_source(
        session, source, maintenance, file_operations, revisions
    )


def _archive_prerequisite_succeeded(
    source: SourceDocument,
    maintenance: Sequence[CoreMaintenanceJob],
    revisions: Sequence[SourceRevision],
) -> bool:
    if source.state != "deleted" or not revisions:
        return False
    current = [
        operation
        for operation in maintenance
        if operation.lifecycle_version == source.lifecycle_version
    ]
    by_revision = {operation.revision_id: operation for operation in current}
    return len(by_revision) == len(revisions) and all(
        revision.id in by_revision and by_revision[revision.id].state == "succeeded"
        for revision in revisions
    )


def _enqueue_archive_locked_source(
    session: AsyncSession,
    source: SourceDocument,
    maintenance: Sequence[CoreMaintenanceJob],
    file_operations: Sequence[SourceFileOperation],
    revisions: Sequence[SourceRevision],
) -> SourceFileOperation | None:
    if not _archive_prerequisite_succeeded(source, maintenance, revisions):
        return None
    if any(
        operation.kind == "restore"
        and operation.lifecycle_version == source.lifecycle_version
        and operation.state != "cancelled"
        for operation in file_operations
    ):
        return None

    operation = next(
        (
            row
            for row in file_operations
            if row.lifecycle_version == source.lifecycle_version and row.kind == "archive"
        ),
        None,
    )
    manifest = _raw_manifest(revisions)
    invalid_manifest = _manifest_error(manifest, source.id)
    if operation is None:
        operation = SourceFileOperation(
            id=uuid.uuid4(),
            source_id=source.id,
            lifecycle_version=source.lifecycle_version,
            kind="archive",
            state="failed" if invalid_manifest else "queued",
            manifest=manifest,
            attempts=0,
            error=_INVALID_MANIFEST_ERROR if invalid_manifest else None,
        )
        session.add(operation)
    elif operation.state == "cancelled":
        if operation.manifest != manifest:
            operation.state = "failed"
            operation.error = _INVALID_MANIFEST_ERROR
            operation.updated_at = func.clock_timestamp()
        else:
            operation.state = "failed" if invalid_manifest else "queued"
            operation.lease_owner = None
            operation.lease_until = None
            operation.error = _INVALID_MANIFEST_ERROR if invalid_manifest else None
            operation.updated_at = func.clock_timestamp()
    return operation


class SourceFileRepository:
    """Persist and execute leases for source archive and restore intentions."""

    def __init__(self, database: ApplicationDatabase, sources: SourceRepository) -> None:
        self.database = database
        self.sources = sources

    async def enqueue_archive_if_cleaned(self, source_id: uuid.UUID) -> dict | None:
        async with self.database.session_factory() as session, session.begin():
            source = await self._lock_source(session, source_id)
            if source is None:
                raise SourceNotFoundError(f"Source {source_id} does not exist")
            operation = await enqueue_archive_for_source(session, source)
            if operation is None:
                return None
            await session.flush()
            return self._public_snapshot(operation)

    async def prepare_restore(
        self,
        source_id: uuid.UUID,
        *,
        expected_lifecycle_version: int,
        expected_latest_revision_id: uuid.UUID | None,
    ) -> dict:
        self._validate_lifecycle(expected_lifecycle_version)
        if expected_latest_revision_id is not None and not isinstance(
            expected_latest_revision_id, uuid.UUID
        ):
            raise ValueError("expected_latest_revision_id must be a UUID or None")

        async with self.database.session_factory() as session, session.begin():
            source = await self._lock_source(session, source_id)
            if source is None:
                raise SourceNotFoundError(f"Source {source_id} does not exist")
            _jobs, maintenance, file_operations, revisions = await _locked_source_rows(
                session, source.id
            )

            if (
                source.state == "active"
                and source.lifecycle_version == expected_lifecycle_version + 1
                and source.latest_revision_id == expected_latest_revision_id
            ):
                completed = next(
                    (
                        operation
                        for operation in file_operations
                        if operation.kind == "restore"
                        and operation.lifecycle_version == expected_lifecycle_version
                        and operation.state == "succeeded"
                        and operation.expected_latest_revision_id == expected_latest_revision_id
                    ),
                    None,
                )
                if completed is not None:
                    snapshot = await self.sources._source_snapshot(session, source)
                    return {
                        "already_restored": True,
                        "source": snapshot,
                        "operation_id": str(completed.id),
                    }
                raise SourceConflictError("Restore operation does not match this source lifecycle")

            if (
                source.state != "deleted"
                or source.lifecycle_version != expected_lifecycle_version
                or source.latest_revision_id != expected_latest_revision_id
            ):
                raise SourceConflictError("Source changed; refresh before restoring")
            if any(operation.state == "running" for operation in maintenance):
                raise SourceConflictError("Core cleanup is still running; retry restore later")
            if any(operation.state == "running" for operation in file_operations):
                raise SourceConflictError("A source file operation is still running")

            now = await self._database_now(session)
            for maintenance_operation in maintenance:
                if maintenance_operation.lifecycle_version != source.lifecycle_version:
                    continue
                if maintenance_operation.state == "queued" or (
                    maintenance_operation.state == "failed"
                    and maintenance_operation.attempts == 0
                ):
                    maintenance_operation.state = "cancelled"
                    maintenance_operation.lease_owner = None
                    maintenance_operation.lease_until = None
                    maintenance_operation.error = None
                    maintenance_operation.updated_at = now

            restore = next(
                (
                    operation
                    for operation in file_operations
                    if operation.kind == "restore"
                    and operation.lifecycle_version == source.lifecycle_version
                ),
                None,
            )
            for operation in file_operations:
                if operation.kind == "archive" and operation.state == "queued":
                    operation.state = "cancelled"
                    operation.lease_owner = None
                    operation.lease_until = None
                    operation.error = None
                    operation.updated_at = now
                elif (
                    operation.kind == "restore"
                    and operation.lifecycle_version != source.lifecycle_version
                    and operation.state == "queued"
                ):
                    operation.state = "cancelled"
                    operation.lease_owner = None
                    operation.lease_until = None
                    operation.error = None
                    operation.updated_at = now

            # Free the per-source pending-operation index before adding/reusing
            # the restore row in the same transaction.
            await session.flush()

            manifest = _raw_manifest(revisions)
            invalid_manifest = _manifest_error(manifest, source.id)
            if restore is None:
                restore = SourceFileOperation(
                    id=uuid.uuid4(),
                    source_id=source.id,
                    lifecycle_version=source.lifecycle_version,
                    kind="restore",
                    state="failed" if invalid_manifest else "queued",
                    manifest=manifest,
                    attempts=0,
                    error=_INVALID_MANIFEST_ERROR if invalid_manifest else None,
                    expected_latest_revision_id=source.latest_revision_id,
                    verified_current_revision_id=source.current_revision_id,
                )
                session.add(restore)
            elif restore.state in {"queued", "failed", "cancelled"}:
                if (
                    restore.manifest != manifest
                    or restore.expected_latest_revision_id != source.latest_revision_id
                    or restore.verified_current_revision_id != source.current_revision_id
                ):
                    raise SourceConflictError(
                        "Restore operation manifest changed; refresh before retrying"
                    )
                restore.state = "failed" if invalid_manifest else "queued"
                restore.lease_owner = None
                restore.lease_until = None
                restore.error = _INVALID_MANIFEST_ERROR if invalid_manifest else None
                restore.updated_at = now
            else:
                raise SourceConflictError(
                    "Restore operation cannot be prepared in its current state"
                )

            await session.flush()
            return {
                "already_restored": False,
                "operation_id": str(restore.id),
                "source_id": str(source.id),
                "lifecycle_version": restore.lifecycle_version,
                "kind": restore.kind,
                "state": restore.state,
                "manifest": [dict(item) for item in restore.manifest]
                if isinstance(restore.manifest, list)
                else restore.manifest,
                "expected_latest_revision_id": str(restore.expected_latest_revision_id)
                if restore.expected_latest_revision_id is not None
                else None,
                "verified_current_revision_id": str(restore.verified_current_revision_id)
                if restore.verified_current_revision_id is not None
                else None,
            }

    async def claim_file_operation(
        self, owner: uuid.UUID, *, operation_id: uuid.UUID | None = None
    ) -> dict | None:
        if not isinstance(owner, uuid.UUID):
            raise ValueError("owner must be a UUID")
        if operation_id is not None and not isinstance(operation_id, uuid.UUID):
            raise ValueError("operation_id must be a UUID or None")

        async with self.database.session_factory() as session, session.begin():
            statement = (
                select(SourceDocument, SourceFileOperation.id)
                .join(SourceFileOperation, SourceFileOperation.source_id == SourceDocument.id)
                .where(
                    SourceDocument.state == "deleted",
                    SourceFileOperation.lifecycle_version == SourceDocument.lifecycle_version,
                    or_(
                        SourceFileOperation.state == "queued",
                        and_(
                            SourceFileOperation.state == "running",
                            or_(
                                SourceFileOperation.lease_until.is_(None),
                                SourceFileOperation.lease_until <= func.clock_timestamp(),
                            ),
                        ),
                    ),
                )
                .order_by(SourceFileOperation.created_at, SourceFileOperation.id)
                .with_for_update(of=SourceDocument, skip_locked=True)
                .limit(1)
            )
            if operation_id is not None:
                statement = statement.where(SourceFileOperation.id == operation_id)
            selected = (await session.execute(statement)).first()
            if selected is None:
                return None
            source, selected_id = selected
            _jobs, maintenance, file_operations, revisions = await _locked_source_rows(
                session, source.id
            )
            operation = next((row for row in file_operations if row.id == selected_id), None)
            if operation is None:
                return None
            now = await self._database_now(session)
            if (
                source.state != "deleted"
                or source.lifecycle_version != operation.lifecycle_version
                or not self._claimable(operation, now)
            ):
                return None
            if operation.kind == "archive":
                if not _archive_prerequisite_succeeded(source, maintenance, revisions):
                    return None
                if any(
                    row.kind == "restore"
                    and row.lifecycle_version == source.lifecycle_version
                    and row.state != "cancelled"
                    for row in file_operations
                ):
                    return None
            else:
                if any(row.state == "running" for row in maintenance):
                    return None
                if (
                    operation.expected_latest_revision_id != source.latest_revision_id
                    or operation.verified_current_revision_id != source.current_revision_id
                ):
                    self._mark_invalid(operation, now)
                    return None

            if not _manifest_matches(operation.manifest, source.id, revisions):
                self._mark_invalid(operation, now)
                return None
            operation.state = "running"
            operation.lease_owner = owner
            operation.lease_until = now + _LEASE_DURATION
            operation.attempts += 1
            operation.error = None
            operation.updated_at = now
            await session.flush()
            return self._claim_snapshot(operation)

    async def renew_file_lease(self, operation_id: uuid.UUID, owner: uuid.UUID) -> bool:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_file_context(session, operation_id)
            if context is None:
                return False
            source, operation, _jobs, _maintenance, _file_operations, _revisions = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, operation, owner, now):
                return False
            operation.lease_until = now + _LEASE_DURATION
            operation.updated_at = now
            return True

    async def complete_file_operation(
        self, operation_id: uuid.UUID, owner: uuid.UUID
    ) -> dict | None:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_file_context(session, operation_id)
            if context is None:
                return None
            source, operation, jobs, maintenance, file_operations, revisions = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, operation, owner, now):
                return None
            if not _manifest_matches(operation.manifest, source.id, revisions):
                return None
            if operation.kind == "archive":
                if not _archive_prerequisite_succeeded(source, maintenance, revisions):
                    return None
                if any(
                    row.kind == "restore"
                    and row.lifecycle_version == source.lifecycle_version
                    and row.state != "cancelled"
                    for row in file_operations
                ):
                    return None
            elif (
                operation.expected_latest_revision_id != source.latest_revision_id
                or operation.verified_current_revision_id != source.current_revision_id
                or operation.kind != "restore"
            ):
                return None

            operation.state = "succeeded"
            operation.lease_owner = None
            operation.lease_until = None
            operation.error = None
            operation.updated_at = now
            if operation.kind == "archive":
                return self._public_snapshot(operation)

            await session.flush()
            source_snapshot = await self.sources._activate_restored_source(
                session,
                source,
                index_jobs=jobs,
                maintenance_jobs=maintenance,
                file_operations=file_operations,
                revisions=revisions,
                expected_latest_revision_id=operation.expected_latest_revision_id,
                verified_current_revision_id=operation.verified_current_revision_id,
                now=now,
                restore_operation=operation,
            )
            return source_snapshot

    async def fail_file_operation(
        self, operation_id: uuid.UUID, owner: uuid.UUID, safe_error: str
    ) -> bool:
        bounded_error = (safe_error or _ARCHIVE_ERROR)[:4000]
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_file_context(session, operation_id)
            if context is None:
                return False
            source, operation, _jobs, _maintenance, _file_operations, _revisions = context
            now = await self._database_now(session)
            if not self._has_live_lease(source, operation, owner, now):
                return False
            operation.state = "failed"
            operation.lease_owner = None
            operation.lease_until = None
            operation.error = bounded_error
            operation.updated_at = now
            return True

    async def release_file_owner(self, owner: uuid.UUID) -> None:
        if not isinstance(owner, uuid.UUID):
            raise ValueError("owner must be a UUID")
        async with self.database.session_factory() as session, session.begin():
            source_ids = list(
                (
                    await session.scalars(
                        select(SourceDocument.id)
                        .join(
                            SourceFileOperation,
                            SourceFileOperation.source_id == SourceDocument.id,
                        )
                        .where(
                            SourceFileOperation.state == "running",
                            SourceFileOperation.lease_owner == owner,
                        )
                        .distinct()
                        .order_by(SourceDocument.id)
                    )
                ).all()
            )
            for source_id in source_ids:
                source = await self._lock_source(session, source_id)
                if source is None:
                    continue
                _jobs, _maintenance, file_operations, _revisions = await _locked_source_rows(
                    session, source.id
                )
                now = await self._database_now(session)
                for operation in file_operations:
                    if operation.state != "running" or operation.lease_owner != owner:
                        continue
                    current = (
                        source.state == "deleted"
                        and source.lifecycle_version == operation.lifecycle_version
                    )
                    operation.state = "queued" if current else "cancelled"
                    operation.lease_owner = None
                    operation.lease_until = None
                    operation.error = None
                    operation.updated_at = now

    async def retry_file_operation(self, operation_id: uuid.UUID) -> dict:
        async with self.database.session_factory() as session, session.begin():
            context = await self._lock_file_context(session, operation_id)
            if context is None:
                raise SourceNotFoundError(f"File operation {operation_id} does not exist")
            source, operation, _jobs, maintenance, file_operations, revisions = context
            if (
                source.state != "deleted"
                or source.lifecycle_version != operation.lifecycle_version
            ):
                raise SourceConflictError("File operation is not for the current deleted cycle")
            if operation.state in {"running", "succeeded", "cancelled"}:
                raise SourceConflictError("File operation cannot be retried in its current state")
            if not _manifest_matches(operation.manifest, source.id, revisions):
                raise SourceConflictError(_INVALID_MANIFEST_ERROR)
            if operation.kind == "archive":
                if not _archive_prerequisite_succeeded(source, maintenance, revisions):
                    raise SourceConflictError("Core cleanup has not completed for every revision")
                if any(
                    row.kind == "restore"
                    and row.lifecycle_version == source.lifecycle_version
                    and row.state != "cancelled"
                    for row in file_operations
                ):
                    raise SourceConflictError("A restore operation is already pending")
            elif (
                operation.expected_latest_revision_id != source.latest_revision_id
                or operation.verified_current_revision_id != source.current_revision_id
            ):
                raise SourceConflictError("Restore source changed; prepare restore again")
            now = await self._database_now(session)
            if operation.state == "failed":
                operation.state = "queued"
                operation.lease_owner = None
                operation.lease_until = None
                operation.error = None
                operation.updated_at = now
            return self._public_snapshot(operation)

    async def list_file_operations(
        self, source_id: uuid.UUID, *, limit: int = 100
    ) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self.database.session_factory() as session:
            exists = await session.get(SourceDocument, source_id)
            if exists is None:
                raise SourceNotFoundError(f"Source {source_id} does not exist")
            rows = await session.scalars(
                select(SourceFileOperation)
                .where(SourceFileOperation.source_id == source_id)
                .order_by(SourceFileOperation.created_at.desc(), SourceFileOperation.id)
                .limit(limit)
            )
            return [self._public_snapshot(operation) for operation in rows]

    async def enqueue_cleaned_sources(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self.database.session_factory() as session, session.begin():
            has_revisions = (
                select(SourceRevision.id)
                .where(SourceRevision.source_id == SourceDocument.id)
                .exists()
            )
            has_revision_without_successful_cleanup = (
                select(SourceRevision.id)
                .where(
                    SourceRevision.source_id == SourceDocument.id,
                    ~select(CoreMaintenanceJob.id)
                    .where(
                        CoreMaintenanceJob.source_id == SourceDocument.id,
                        CoreMaintenanceJob.revision_id == SourceRevision.id,
                        CoreMaintenanceJob.lifecycle_version == SourceDocument.lifecycle_version,
                        CoreMaintenanceJob.state == "succeeded",
                    )
                    .exists(),
                )
                .exists()
            )
            current_archive_intent_exists = (
                select(SourceFileOperation.id)
                .where(
                    SourceFileOperation.source_id == SourceDocument.id,
                    SourceFileOperation.lifecycle_version == SourceDocument.lifecycle_version,
                    SourceFileOperation.kind == "archive",
                    SourceFileOperation.state != "cancelled",
                )
                .exists()
            )
            current_restore_intent_exists = (
                select(SourceFileOperation.id)
                .where(
                    SourceFileOperation.source_id == SourceDocument.id,
                    SourceFileOperation.lifecycle_version == SourceDocument.lifecycle_version,
                    SourceFileOperation.kind == "restore",
                    SourceFileOperation.state != "cancelled",
                )
                .exists()
            )
            candidate_ids = list(
                (
                    await session.scalars(
                        select(SourceDocument.id)
                        .where(
                            SourceDocument.state == "deleted",
                            has_revisions,
                            ~has_revision_without_successful_cleanup,
                            ~current_archive_intent_exists,
                            ~current_restore_intent_exists,
                        )
                        .order_by(SourceDocument.created_at, SourceDocument.id)
                        .limit(limit)
                    )
                ).all()
            )
            # Selection is oldest-first for bounded progress; row acquisition
            # follows the UUID order used by owner-release paths.
            candidate_ids.sort(key=lambda source_id: source_id.int)
            enqueued = 0
            for source_id in candidate_ids:
                source = await self._lock_source(session, source_id)
                if source is None:
                    continue
                _jobs, maintenance, file_operations, revisions = await _locked_source_rows(
                    session, source.id
                )
                before = next(
                    (
                        row
                        for row in file_operations
                        if row.kind == "archive"
                        and row.lifecycle_version == source.lifecycle_version
                    ),
                    None,
                )
                previous_state = before.state if before is not None else None
                operation = _enqueue_archive_locked_source(
                    session, source, maintenance, file_operations, revisions
                )
                if operation is not None and (
                    previous_state is None
                    or (previous_state == "cancelled" and operation.state != "cancelled")
                ):
                    enqueued += 1
            return enqueued

    async def _lock_source(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> SourceDocument | None:
        return await session.scalar(
            select(SourceDocument).where(SourceDocument.id == source_id).with_for_update()
        )

    async def _lock_file_context(
        self, session: AsyncSession, operation_id: uuid.UUID
    ) -> tuple[
        SourceDocument,
        SourceFileOperation,
        list[Job],
        list[CoreMaintenanceJob],
        list[SourceFileOperation],
        list[SourceRevision],
    ] | None:
        source_id = await session.scalar(
            select(SourceFileOperation.source_id).where(SourceFileOperation.id == operation_id)
        )
        if source_id is None:
            return None
        source = await self._lock_source(session, source_id)
        if source is None:
            raise SourceConflictError("File operation references a missing source")
        jobs, maintenance, file_operations, revisions = await _locked_source_rows(
            session, source.id
        )
        operation = next((row for row in file_operations if row.id == operation_id), None)
        if operation is None:
            return None
        return source, operation, jobs, maintenance, file_operations, revisions

    @staticmethod
    def _public_snapshot(operation: SourceFileOperation) -> dict:
        return {
            "operation_id": str(operation.id),
            "source_id": str(operation.source_id),
            "lifecycle_version": operation.lifecycle_version,
            "kind": operation.kind,
            "state": operation.state,
            "attempts": operation.attempts,
            "error": operation.error,
            "created_at": SourceFileRepository._iso(operation.created_at),
            "updated_at": SourceFileRepository._iso(operation.updated_at),
            "lease_until": SourceFileRepository._iso(operation.lease_until),
        }

    @classmethod
    def _claim_snapshot(cls, operation: SourceFileOperation) -> dict:
        return {
            "operation_id": str(operation.id),
            "source_id": str(operation.source_id),
            "lifecycle_version": operation.lifecycle_version,
            "kind": operation.kind,
            "manifest": [dict(item) for item in operation.manifest],
            "expected_latest_revision_id": str(operation.expected_latest_revision_id)
            if operation.expected_latest_revision_id is not None
            else None,
            "verified_current_revision_id": str(operation.verified_current_revision_id)
            if operation.verified_current_revision_id is not None
            else None,
        }

    @staticmethod
    def _mark_invalid(operation: SourceFileOperation, now: datetime) -> None:
        operation.state = "failed"
        operation.lease_owner = None
        operation.lease_until = None
        operation.error = _INVALID_MANIFEST_ERROR
        operation.updated_at = now

    @staticmethod
    def _validate_lifecycle(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("expected_lifecycle_version must be a nonnegative integer")

    @staticmethod
    def _claimable(operation: SourceFileOperation, now: datetime) -> bool:
        return operation.state == "queued" or (
            operation.state == "running"
            and (operation.lease_until is None or operation.lease_until <= now)
        )

    @staticmethod
    def _has_live_lease(
        source: SourceDocument,
        operation: SourceFileOperation,
        owner: uuid.UUID,
        now: datetime,
    ) -> bool:
        return (
            source.state == "deleted"
            and source.lifecycle_version == operation.lifecycle_version
            and operation.state == "running"
            and operation.lease_owner == owner
            and operation.lease_until is not None
            and operation.lease_until > now
        )

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        value = await session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("PostgreSQL did not return the database clock")
        return value

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
