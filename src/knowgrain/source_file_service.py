"""Execute durable source archive and restore operations against the Vault."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from typing import Any
from uuid import UUID, uuid4

from knowgrain.source_archive_files import (
    ArchiveEntry,
    SourceArchiveFileError,
    SourceArchiveFiles,
)
from knowgrain.source_repository import SourceConflictError, SourceNotFoundError
from knowgrain.source_service import SourceLifecycleUnavailableError
from knowgrain.source_file_repository import SourceFileRepository

logger = logging.getLogger(__name__)

_SOURCE_FILE_CONFLICT_MESSAGE = "Vault 中的原件缺失或已变化，无法恢复资料；请检查文件后重试"
_SOURCE_FILE_UNAVAILABLE_MESSAGE = "Vault 原件暂不可安全读取，请检查 Vault 状态与权限后重试"


class SourceFileLeaseLostError(RuntimeError):
    """The current executor can no longer safely finish its file operation."""


class SourceFileService:
    """Bridge durable file-operation rows to verified Vault directory moves.

    ``file_lock`` must be shared with ``SourceFileRunner`` and other app-level
    operations that can write source files. The service acquires it exactly
    once per operation; callers must not acquire it around these methods.
    """

    def __init__(
        self,
        repository: SourceFileRepository,
        archive_files: SourceArchiveFiles,
        file_lock: asyncio.Lock,
    ) -> None:
        if not isinstance(archive_files, SourceArchiveFiles):
            raise TypeError("SourceFileService requires SourceArchiveFiles")
        if not isinstance(file_lock, asyncio.Lock):
            raise TypeError("SourceFileService requires a shared asyncio.Lock")
        self.repository = repository
        self.archive_files = archive_files
        self.file_lock = file_lock
        # One service instance owns at most one operation at a time. A stable
        # owner lets cancellation and shutdown return unfinished work to queue.
        self.owner = uuid4()

    async def stop(self) -> None:
        """Wait for any shared-lock file work to drain, then release this owner."""
        async with self.file_lock:
            await self._release_owner()

    async def restore_source(
        self,
        source_id: UUID,
        *,
        expected_lifecycle_version: int,
        expected_latest_revision_id: UUID | None,
    ) -> dict:
        """Persist a restore intent, move every registered revision, then activate."""
        async with self.file_lock:
            prepared = await self.repository.prepare_restore(
                source_id,
                expected_lifecycle_version=expected_lifecycle_version,
                expected_latest_revision_id=expected_latest_revision_id,
            )
            if prepared.get("already_restored") is True:
                snapshot = prepared.get("source")
                if isinstance(snapshot, dict):
                    return snapshot
                raise SourceLifecycleUnavailableError(_SOURCE_FILE_UNAVAILABLE_MESSAGE)

            operation_id = _operation_id(prepared)
            claimed, result = await self._claim_and_execute_locked(
                operation_id, propagate=True
            )
            if not claimed:
                raise SourceConflictError("Source restore operation could not be claimed")
            if not isinstance(result, dict):
                raise SourceLifecycleUnavailableError(_SOURCE_FILE_UNAVAILABLE_MESSAGE)
            return result

    async def run_next(self, operation_id: UUID | None = None) -> bool:
        """Claim and execute one queued file operation, if one is available."""
        try:
            async with self.file_lock:
                claimed, result = await self._claim_and_execute_locked(
                    operation_id, propagate=False
                )
            return claimed and result is not None
        except (SourceNotFoundError, SourceConflictError, SourceLifecycleUnavailableError):
            # A background operation records its safe failure in the durable log.
            # Tell the runner to back off for a poll interval before retrying.
            return False

    async def _claim_and_execute_locked(
        self, operation_id: UUID | None, *, propagate: bool
    ) -> tuple[bool, dict | None]:
        """Claim, execute, and release while the caller holds ``file_lock``."""
        claimed: dict | None = None
        try:
            claimed = await self.repository.claim_file_operation(
                self.owner, operation_id=operation_id
            )
            if claimed is None:
                return False, None
            result = await self._execute_claimed(claimed, propagate=propagate)
            return True, result
        finally:
            # Keep owner release inside the same lock. A second request sharing
            # this owner must never claim work before the prior release finishes.
            if claimed is not None:
                await self._release_owner()

    async def _execute_claimed(self, operation: dict, *, propagate: bool) -> dict | None:
        operation_id = _operation_id(operation)
        source_id = _uuid_value(operation.get("source_id"))
        kind = operation.get("kind")
        if kind not in {"archive", "restore"}:
            raise SourceLifecycleUnavailableError(_SOURCE_FILE_UNAVAILABLE_MESSAGE)
        entries = _archive_entries(operation.get("manifest"))

        # Claim and move are fenced under the shared lock. Recheck ownership
        # after acquiring it and immediately before dispatching filesystem work.
        try:
            renewed = await self.repository.renew_file_lease(operation_id, self.owner)
        except Exception:
            renewed = None
        if renewed is not True:
            if propagate:
                raise SourceLifecycleUnavailableError(
                    _SOURCE_FILE_UNAVAILABLE_MESSAGE
                ) from None
            logger.warning("Source file operation %s lost its lease before moving", operation_id)
            return None

        moving = asyncio.create_task(
            self._move_and_verify(source_id, entries, kind),
            name=f"source-file-{kind}-{operation_id}",
        )
        renewal = asyncio.create_task(
            self._renew_while_running(operation_id),
            name=f"source-file-lease-{operation_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {moving, renewal}, return_when=asyncio.FIRST_COMPLETED
            )
            if renewal in done:
                try:
                    await renewal
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise SourceFileLeaseLostError from exc
                raise SourceFileLeaseLostError

            await moving
            # Refresh the lease immediately before completion. The periodic
            # renewal task is then drained so no uncertain renewal can race the
            # database completion transaction.
            if renewal.done():
                raise SourceFileLeaseLostError
            try:
                renewed = await self.repository.renew_file_lease(operation_id, self.owner)
            except Exception as exc:
                raise SourceFileLeaseLostError from exc
            if renewed is not True or renewal.done():
                raise SourceFileLeaseLostError
            renewal.cancel()
            if await _join_task(renewal):
                raise asyncio.CancelledError

            try:
                completed = await self.repository.complete_file_operation(
                    operation_id, self.owner
                )
            except SourceNotFoundError:
                raise
            except SourceConflictError:
                # A moved directory with a stale source cycle remains journaled;
                # the repository must not let this executor activate another cycle.
                raise
            except Exception as exc:
                # The move may already be durable. Releasing the owner returns the
                # journal to replay, where actual location is rechecked idempotently.
                raise SourceLifecycleUnavailableError(
                    _SOURCE_FILE_UNAVAILABLE_MESSAGE
                ) from exc
            if completed is None:
                raise SourceFileLeaseLostError
            return completed if isinstance(completed, dict) else None
        except asyncio.CancelledError:
            moving.cancel()
            await _join_task(moving)
            raise
        except SourceFileLeaseLostError:
            moving.cancel()
            if await _join_task(moving):
                raise asyncio.CancelledError
            if propagate:
                raise SourceLifecycleUnavailableError(
                    _SOURCE_FILE_UNAVAILABLE_MESSAGE
                ) from None
            logger.warning("Source file operation %s lost its lease", operation_id)
            return None
        except (SourceNotFoundError, SourceConflictError, SourceLifecycleUnavailableError):
            moving.cancel()
            if await _join_task(moving):
                raise asyncio.CancelledError
            if not renewal.done():
                renewal.cancel()
            if await _join_task(renewal):
                raise asyncio.CancelledError
            if propagate:
                raise
            return None
        except Exception as exc:
            moving.cancel()
            if await _join_task(moving):
                raise asyncio.CancelledError
            if renewal.done():
                try:
                    await renewal
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if propagate:
                        raise SourceLifecycleUnavailableError(
                            _SOURCE_FILE_UNAVAILABLE_MESSAGE
                        ) from None
                    logger.warning("Source file operation %s lost its lease", operation_id)
                    return None
            safe_error = f"原件文件操作失败 ({type(exc).__name__})；检查 Vault 后重试"
            try:
                failed = await self.repository.fail_file_operation(
                    operation_id, self.owner, safe_error
                )
            except Exception as record_error:
                logger.warning(
                    "Source file operation %s failure could not be recorded (%s)",
                    operation_id,
                    type(record_error).__name__,
                )
                if propagate:
                    raise SourceLifecycleUnavailableError(
                        _SOURCE_FILE_UNAVAILABLE_MESSAGE
                    ) from None
                return None
            if failed is not True:
                if propagate:
                    raise SourceLifecycleUnavailableError(
                        _SOURCE_FILE_UNAVAILABLE_MESSAGE
                    ) from None
                return None
            if propagate:
                if isinstance(exc, SourceArchiveFileError):
                    if exc.code in {"missing", "conflict"}:
                        raise SourceConflictError(_SOURCE_FILE_CONFLICT_MESSAGE) from None
                    raise SourceLifecycleUnavailableError(
                        _SOURCE_FILE_UNAVAILABLE_MESSAGE
                    ) from None
                raise SourceLifecycleUnavailableError(
                    _SOURCE_FILE_UNAVAILABLE_MESSAGE
                ) from None
            return None
        finally:
            if not renewal.done():
                renewal.cancel()
            if await _join_task(renewal):
                raise asyncio.CancelledError

    async def _move_and_verify(
        self,
        source_id: UUID,
        entries: tuple[ArchiveEntry, ...],
        kind: str,
    ) -> None:
        move = self.archive_files.archive if kind == "archive" else self.archive_files.restore
        await _thread_call_drained(move, source_id, entries)
        actual = await _thread_call_drained(self.archive_files.location, source_id, entries)
        expected = "trash" if kind == "archive" else "vault"
        if actual != expected:
            raise SourceArchiveFileError("conflict")

    async def _renew_while_running(self, operation_id: UUID) -> None:
        while True:
            await asyncio.sleep(20)
            renewed = await self.repository.renew_file_lease(operation_id, self.owner)
            if renewed is not True:
                raise SourceFileLeaseLostError

    async def _release_owner(self) -> None:
        release = asyncio.create_task(self.repository.release_file_owner(self.owner))
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            await _join_task(release)
            raise
        except Exception as exc:
            logger.warning(
                "Source file operation owner release failed (%s); its lease will expire",
                type(exc).__name__,
            )


def _operation_id(row: dict) -> UUID:
    raw = row.get("operation_id", row.get("id"))
    return _uuid_value(raw)


def _uuid_value(raw: Any) -> UUID:
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        raise SourceLifecycleUnavailableError(_SOURCE_FILE_UNAVAILABLE_MESSAGE) from None


def _archive_entries(raw_manifest: Any) -> tuple[ArchiveEntry, ...]:
    if (
        not isinstance(raw_manifest, (list, tuple))
        or not 1 <= len(raw_manifest) <= 10_000
    ):
        raise SourceLifecycleUnavailableError(_SOURCE_FILE_UNAVAILABLE_MESSAGE)
    entries: list[ArchiveEntry] = []
    for raw in raw_manifest:
        if not isinstance(raw, dict):
            raise SourceLifecycleUnavailableError(_SOURCE_FILE_UNAVAILABLE_MESSAGE)
        revision_id = raw.get("revision_id")
        try:
            revision_id = (
                revision_id if isinstance(revision_id, UUID) else UUID(str(revision_id))
            )
        except (ValueError, TypeError, AttributeError):
            raise SourceLifecycleUnavailableError(
                _SOURCE_FILE_UNAVAILABLE_MESSAGE
            ) from None
        entries.append(
            ArchiveEntry(
                revision_id=revision_id,
                vault_path=raw.get("vault_path"),
                sha256=raw.get("sha256"),
            )
        )
    return tuple(entries)


async def _thread_call_drained(function, *args):
    """Run blocking filesystem work and wait for its thread on cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
            if not worker.done():
                continue
            with suppress(BaseException):
                worker.result()
            raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


async def _join_task(task: asyncio.Task) -> bool:
    """Join a child despite repeated cancellation; return whether it arrived."""
    joined = asyncio.gather(task, return_exceptions=True)
    cancelled = False
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled
