"""Inert rebuild ledger: persistent ownership and intents, no Core activation.

Audit-success/absence transitions deliberately await the strict Core auditor.
Normal application startup and APIs do not call this repository yet.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowgrain.database import ApplicationDatabase
from knowgrain.index_fence import (
    IndexConflict,
    ItemGrant,
    LeaseLost,
    OperationGrant,
    QuiescenceGuard,
    database_now,
)
from knowgrain.index_profile import IndexProfile, ProfileValidationError
from knowgrain.models import (
    CoreGeneration,
    CoreGenerationRevision,
    CoreMaintenanceJob,
    CoreSelector,
    GenerationJob,
    Job,
    QueryJob,
    RebuildItem,
    RebuildOperation,
    SourceDocument,
    SourceFileOperation,
    SourceRevision,
    VaultBinding,
)

_LEASE = timedelta(seconds=90)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ERRORS = frozenset(
    {
        "quiescence_failed",
        "snapshot_invalid",
        "provider_unavailable",
        "core_failure",
        "source_changed",
        "lease_lost",
        "audit_failed",
        "restart_required",
    }
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _version(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("Expected version must be a nonnegative integer")


def _chunks(value: list[str]) -> list[str]:
    if (
        type(value) is not list
        or len(value) > 10000
        or any(
            type(item) is not str
            or not 1 <= len(item) <= 512
            or any(ord(character) < 32 for character in item)
            for item in value
        )
    ):
        raise ValueError("Cleanup manifest is invalid or exceeds its supported limit")
    return sorted(set(value))


class RebuildRepository:
    def __init__(self, database: ApplicationDatabase):
        self.database = database

    @staticmethod
    def _view(operation: RebuildOperation) -> dict:
        return {
            "operation_id": operation.id,
            "target_generation_id": operation.target_generation_id,
            "state": operation.state,
            "version": operation.version,
            "snapshot_count": operation.snapshot_count,
            "attempts": operation.attempts,
            "error": operation.error,
        }

    async def status(self, operation_id: UUID) -> dict:
        async with self.database.session_factory() as session:
            operation = await session.get(RebuildOperation, operation_id)
            if operation is None:
                raise IndexConflict("Rebuild operation does not exist")
            return self._view(operation)

    @staticmethod
    async def _selector(session: AsyncSession) -> CoreSelector:
        selector = await session.scalar(
            select(CoreSelector).where(CoreSelector.id == 1).with_for_update()
        )
        if selector is None:
            raise IndexConflict("Index selector has not been initialized")
        return selector

    @staticmethod
    async def _operation(session: AsyncSession, operation_id: UUID) -> RebuildOperation:
        operation = await session.scalar(
            select(RebuildOperation).where(RebuildOperation.id == operation_id).with_for_update()
        )
        if operation is None:
            raise IndexConflict("Rebuild operation does not exist")
        return operation

    @staticmethod
    def _pending(selector: CoreSelector, operation: RebuildOperation) -> None:
        if (
            not selector.frozen
            or selector.pending_rebuild_id != operation.id
            or selector.active_generation_id != operation.old_generation_id
            or selector.restart_required
        ):
            raise IndexConflict("Rebuild is not the admitted pending operation")

    @staticmethod
    async def _binding(session: AsyncSession, operation: RebuildOperation) -> None:
        binding = await session.scalar(
            select(VaultBinding).where(VaultBinding.id == 1).with_for_update(read=True)
        )
        if (
            binding is None
            or binding.binding_id != operation.vault_binding_id
            or binding.root_path != operation.vault_root_path
        ):
            raise IndexConflict("Rebuild Vault binding has changed")

    @staticmethod
    async def _target(session: AsyncSession, operation: RebuildOperation) -> None:
        target = await session.scalar(
            select(CoreGeneration)
            .where(CoreGeneration.id == operation.target_generation_id)
            .with_for_update(read=True)
        )
        if target is None or target.config_status != "sealed":
            raise IndexConflict("Target generation configuration is not sealed")
        try:
            profile = IndexProfile.from_canonical_json(target.canonical_profile)
        except ProfileValidationError:
            raise IndexConflict("Target generation profile is invalid") from None
        for name in ("content_embedding", "graph_write", "llm", "snapshot"):
            if getattr(target, name + "_fingerprint") != getattr(profile, name + "_fingerprint"):
                raise IndexConflict("Target generation fingerprint differs from its profile")
        if operation.request_sha256 != _digest(
            {
                "expected_selector_version": operation.expected_selector_version,
                "profile": profile.snapshot_fingerprint,
                "vault_binding_id": str(operation.vault_binding_id),
                "working_parent": str(Path(target.working_dir).parent),
            }
        ):
            raise IndexConflict("Target generation differs from the original request")

    @staticmethod
    def _owned(operation: RebuildOperation, grant: OperationGrant, now) -> None:
        if (
            operation.id != grant.operation_id
            or operation.target_generation_id != grant.generation_id
            or operation.lease_owner != grant.owner
            or operation.claim_token != grant.claim_token
            or operation.claim_fence != grant.claim_fence
            or operation.lease_until is None
            or operation.lease_until <= now
            or operation.state not in {"preparing", "building", "verifying"}
        ):
            raise LeaseLost("Rebuild operation ownership is no longer valid")

    async def request_rebuild(
        self,
        operation_id: UUID,
        *,
        expected_selector_version: int,
        target_profile: IndexProfile,
        vault_binding_id: UUID,
        working_parent: Path,
    ) -> dict:
        _version(expected_selector_version)
        if (
            not isinstance(operation_id, UUID)
            or not isinstance(vault_binding_id, UUID)
            or type(target_profile) is not IndexProfile
            or not isinstance(working_parent, Path)
        ):
            raise ValueError("Rebuild request types are invalid")
        parent = working_parent.resolve()
        request_hash = _digest(
            {
                "expected_selector_version": expected_selector_version,
                "profile": target_profile.snapshot_fingerprint,
                "vault_binding_id": str(vault_binding_id),
                "working_parent": str(parent),
            }
        )
        async with self.database.session_factory() as session, session.begin():
            selector = await self._selector(session)
            existing = await session.get(RebuildOperation, operation_id)
            if existing is not None:
                if existing.request_sha256 != request_hash:
                    raise IndexConflict("Operation identity was reused with a different request")
                return self._view(existing)
            if (
                selector.frozen
                or selector.pending_rebuild_id is not None
                or selector.restart_required
                or selector.version != expected_selector_version
            ):
                raise IndexConflict("Index selector changed or admission is frozen")
            binding = await session.scalar(
                select(VaultBinding).where(VaultBinding.id == 1).with_for_update(read=True)
            )
            if binding is None or binding.binding_id != vault_binding_id:
                raise IndexConflict("Rebuild Vault binding has changed")
            generation_id = uuid4()
            working_dir = str(parent / generation_id.hex)
            if len(working_dir) > 4096:
                raise ValueError("Generation working path exceeds the supported length")
            generation = CoreGeneration(
                id=generation_id,
                workspace="kg_" + generation_id.hex,
                vector_model_name="kg_" + secrets.token_hex(12),
                working_dir=working_dir,
                config_status="sealed",
                canonical_profile=target_profile.to_canonical_json(),
                content_embedding_fingerprint=target_profile.content_embedding_fingerprint,
                graph_write_fingerprint=target_profile.graph_write_fingerprint,
                llm_fingerprint=target_profile.llm_fingerprint,
                snapshot_fingerprint=target_profile.snapshot_fingerprint,
            )
            session.add(generation)
            await session.flush()
            operation = RebuildOperation(
                id=operation_id,
                old_generation_id=selector.active_generation_id,
                target_generation_id=generation_id,
                expected_selector_version=expected_selector_version,
                vault_binding_id=binding.binding_id,
                vault_root_path=binding.root_path,
                request_sha256=request_hash,
            )
            session.add(operation)
            await session.flush()
            selector.pending_rebuild_id = operation_id
            selector.frozen = True
            selector.version += 1
            selector.updated_at = await database_now(session)
            return self._view(operation)

    async def claim_operation(self, operation_id: UUID, owner: UUID) -> OperationGrant | None:
        async with self.database.session_factory() as session, session.begin():
            selector = await self._selector(session)
            operation = await self._operation(session, operation_id)
            self._pending(selector, operation)
            await self._binding(session, operation)
            await self._target(session, operation)
            now = await database_now(session)
            if operation.state not in {"queued", "preparing", "building", "verifying"} or (
                operation.lease_until is not None and operation.lease_until > now
            ):
                return None
            if operation.state == "queued":
                operation.state = (
                    "building" if operation.snapshot_sha256 is not None else "preparing"
                )
            operation.claim_token = uuid4()
            operation.claim_fence += 1
            operation.lease_owner = owner
            operation.lease_until = now + _LEASE
            operation.attempts += 1
            operation.version += 1
            operation.updated_at = now
            return OperationGrant(
                operation.id,
                operation.target_generation_id,
                owner,
                operation.claim_token,
                operation.claim_fence,
            )

    async def renew_operation(self, grant: OperationGrant) -> None:
        async with self.database.session_factory() as session, session.begin():
            selector = await self._selector(session)
            operation = await self._operation(session, grant.operation_id)
            self._pending(selector, operation)
            await self._binding(session, operation)
            now = await database_now(session)
            self._owned(operation, grant, now)
            operation.lease_until = now + _LEASE
            operation.updated_at = now

    async def fail_operation(
        self, grant: OperationGrant, error_code: str, *, restart_required: bool = False
    ) -> None:
        if error_code not in _ERRORS or type(restart_required) is not bool:
            raise ValueError("Rebuild error code is unsupported")
        async with self.database.session_factory() as session, session.begin():
            selector = await self._selector(session)
            operation = await self._operation(session, grant.operation_id)
            self._pending(selector, operation)
            now = await database_now(session)
            self._owned(operation, grant, now)
            operation.state = "failed"
            operation.error = error_code
            operation.claim_token = operation.lease_owner = operation.lease_until = None
            operation.version += 1
            operation.updated_at = now
            if restart_required:
                selector.restart_required = True
                selector.error = "restart_required"
                selector.updated_at = now

    async def retry(self, operation_id: UUID, *, expected_version: int) -> dict:
        _version(expected_version)
        async with self.database.session_factory() as session, session.begin():
            selector = await self._selector(session)
            operation = await self._operation(session, operation_id)
            self._pending(selector, operation)
            await self._binding(session, operation)
            if operation.retry_from_version == expected_version:
                return self._view(operation)
            if operation.version != expected_version or operation.state != "failed":
                raise IndexConflict("Rebuild retry version or state changed")
            operation.retry_from_version = expected_version
            operation.version += 1
            operation.state = "queued"
            operation.error = None
            operation.claim_token = operation.lease_owner = operation.lease_until = None
            operation.updated_at = await database_now(session)
            return self._view(operation)

    async def seal_snapshot(self, grant: OperationGrant, quiescence: QuiescenceGuard) -> str:
        if not isinstance(quiescence, QuiescenceGuard):
            raise TypeError("Snapshot sealing requires a coordinator quiescence guard")
        async with self.database.session_factory() as session, session.begin():
            selector = await self._selector(session)
            operation = await self._operation(session, grant.operation_id)
            self._pending(selector, operation)
            await self._binding(session, operation)
            if operation.snapshot_sha256 is not None:
                self._owned(operation, grant, await database_now(session))
                if selector.execution_epoch != operation.snapshot_execution_epoch:
                    raise IndexConflict("Sealed snapshot epoch differs from selector")
                return operation.snapshot_sha256
            await self._target(session, operation)
            if operation.state != "preparing":
                raise IndexConflict("Rebuild is not preparing a snapshot")
            quiescence.assert_quiescent(
                operation.id, operation.old_generation_id, selector.execution_epoch
            )
            sources = (
                await session.scalars(
                    select(SourceDocument).order_by(SourceDocument.id).with_for_update()
                )
            ).all()
            # A running row blocks sealing even after lease expiry. Queued tasks
            # and prepared review journals remain retained for coordinator replay.
            for model in (Job, CoreMaintenanceJob, SourceFileOperation, GenerationJob, QueryJob):
                rows = (
                    await session.scalars(select(model).order_by(model.id).with_for_update())
                ).all()
                if any(row.state == "running" for row in rows):
                    raise IndexConflict("Ordinary execution has not drained")
            revisions = (
                await session.scalars(
                    select(SourceRevision)
                    .order_by(SourceRevision.source_id, SourceRevision.id)
                    .with_for_update()
                )
            ).all()
            by_revision = {row.id: row for row in revisions}
            snapshot = []
            for source in sources:
                if source.state != "active":
                    continue
                revision = by_revision.get(source.latest_revision_id)
                if revision is None or revision.source_id != source.id:
                    raise IndexConflict("Active source has no valid latest revision")
                snapshot.append(
                    {
                        "source_id": str(source.id),
                        "revision_id": str(revision.id),
                        "lifecycle_version": source.lifecycle_version,
                        "filename": revision.filename,
                        "media_type": revision.media_type,
                        "vault_path": revision.vault_path,
                        "source_sha256": revision.sha256,
                        "original_parsed_text_sha256": revision.parsed_text_sha256,
                        "original_parsed_segments": revision.parsed_segments,
                        "original_indexed_at": revision.indexed_at.isoformat()
                        if revision.indexed_at
                        else None,
                    }
                )
            # Revalidate after all potentially waiting source/task/revision locks.
            quiescence.assert_quiescent(
                operation.id, operation.old_generation_id, selector.execution_epoch
            )
            now = await database_now(session)
            self._owned(operation, grant, now)
            if (
                await session.scalar(
                    select(RebuildItem.id).where(RebuildItem.operation_id == operation.id).limit(1)
                )
                is not None
            ):
                raise IndexConflict("Unsealed operation already contains snapshot items")
            snapshot_hash = _digest(snapshot)
            selector.execution_epoch += 1
            selector.updated_at = now
            operation.snapshot_sha256 = snapshot_hash
            operation.snapshot_count = len(snapshot)
            operation.snapshot_execution_epoch = selector.execution_epoch
            operation.state = "building"
            operation.version += 1
            operation.updated_at = now
            for data in snapshot:
                revision = by_revision[UUID(data["revision_id"])]
                session.add(
                    RebuildItem(
                        operation_id=operation.id,
                        generation_id=operation.target_generation_id,
                        source_id=revision.source_id,
                        revision_id=revision.id,
                        lifecycle_version=data["lifecycle_version"],
                        filename=revision.filename,
                        media_type=revision.media_type,
                        vault_path=revision.vault_path,
                        source_sha256=revision.sha256,
                        original_parsed_text_sha256=revision.parsed_text_sha256,
                        original_parsed_segments=revision.parsed_segments,
                        original_indexed_at=revision.indexed_at,
                        snapshot_sha256=snapshot_hash,
                    )
                )
            for source in sources:
                if source.state == "active":
                    source.current_revision_id = None
            return snapshot_hash

    async def list_items(
        self, operation_id: UUID, *, limit: int = 100, after: UUID | None = None
    ) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Item page size must be between 1 and 500")
        async with self.database.session_factory() as session:
            statement = select(RebuildItem).where(RebuildItem.operation_id == operation_id)
            if after is not None:
                statement = statement.where(RebuildItem.id > after)
            rows = (await session.scalars(statement.order_by(RebuildItem.id).limit(limit))).all()
            return [
                {
                    "item_id": row.id,
                    "source_id": row.source_id,
                    "revision_id": row.revision_id,
                    "state": row.state,
                    "attempts": row.attempts,
                    "error": row.error,
                }
                for row in rows
            ]

    async def _item_context(self, session: AsyncSession, parent: OperationGrant, item_id: UUID):
        selector = await self._selector(session)
        operation = await self._operation(session, parent.operation_id)
        self._pending(selector, operation)
        await self._binding(session, operation)
        await self._target(session, operation)
        if (
            operation.state != "building"
            or operation.snapshot_sha256 is None
            or operation.snapshot_execution_epoch != selector.execution_epoch
        ):
            raise LeaseLost("Rebuild snapshot is no longer admitted")
        identity = (
            await session.execute(
                select(RebuildItem.source_id, RebuildItem.revision_id).where(
                    RebuildItem.id == item_id, RebuildItem.operation_id == operation.id
                )
            )
        ).first()
        if identity is None:
            raise IndexConflict("Rebuild item does not belong to this operation")
        source = await session.scalar(
            select(SourceDocument).where(SourceDocument.id == identity.source_id).with_for_update()
        )
        revision = await session.scalar(
            select(SourceRevision)
            .where(SourceRevision.id == identity.revision_id)
            .with_for_update()
        )
        item = await session.scalar(
            select(RebuildItem).where(RebuildItem.id == item_id).with_for_update()
        )
        member = await session.scalar(
            select(CoreGenerationRevision)
            .where(
                CoreGenerationRevision.generation_id == operation.target_generation_id,
                CoreGenerationRevision.revision_id == identity.revision_id,
            )
            .with_for_update()
        )
        now = await database_now(session)
        self._owned(operation, parent, now)
        if (
            source is None
            or revision is None
            or item is None
            or source.state != "active"
            or source.latest_revision_id != revision.id
            or source.lifecycle_version != item.lifecycle_version
            or revision.source_id != source.id
            or revision.sha256 != item.source_sha256
            or revision.vault_path != item.vault_path
            or item.generation_id != parent.generation_id
            or item.snapshot_sha256 != operation.snapshot_sha256
        ):
            raise IndexConflict("Rebuild source or snapshot has changed")
        return selector, operation, item, member, now

    @staticmethod
    def _item_owned(selector, operation, item, grant: ItemGrant, now) -> None:
        if (
            item.id != grant.item_id
            or item.source_id != grant.source_id
            or item.revision_id != grant.revision_id
            or selector.execution_epoch != grant.execution_epoch
            or item.snapshot_sha256 != grant.snapshot_sha256
            or item.state != "running"
            or item.lease_owner != grant.owner
            or item.claim_token != grant.claim_token
            or item.claim_fence != grant.claim_fence
            or item.parent_claim_token != operation.claim_token
            or item.parent_claim_fence != operation.claim_fence
            or item.lease_until is None
            or item.lease_until <= now
        ):
            raise LeaseLost("Rebuild item ownership is no longer valid")

    async def claim_item(
        self, parent: OperationGrant, item_id: UUID, owner: UUID
    ) -> ItemGrant | None:
        async with self.database.session_factory() as session, session.begin():
            selector, _operation, item, _member, now = await self._item_context(
                session, parent, item_id
            )
            if item.state == "verified":
                return None
            same_parent = (
                item.parent_claim_token == parent.claim_token
                and item.parent_claim_fence == parent.claim_fence
            )
            if (
                item.state == "running"
                and same_parent
                and item.lease_until is not None
                and item.lease_until > now
            ):
                return None
            item.state = "running"
            item.claim_token = uuid4()
            item.claim_fence += 1
            item.lease_owner = owner
            item.lease_until = now + _LEASE
            item.parent_claim_token = parent.claim_token
            item.parent_claim_fence = parent.claim_fence
            item.attempts += 1
            item.error = None
            item.updated_at = now
            return ItemGrant(
                item.id,
                item.source_id,
                item.revision_id,
                parent,
                selector.execution_epoch,
                item.snapshot_sha256,
                owner,
                item.claim_token,
                item.claim_fence,
            )

    async def renew_item(self, grant: ItemGrant) -> None:
        async with self.database.session_factory() as session, session.begin():
            selector, operation, item, _member, now = await self._item_context(
                session, grant.parent, grant.item_id
            )
            self._item_owned(selector, operation, item, grant, now)
            item.lease_until = now + _LEASE
            item.updated_at = now

    async def begin_write(
        self, grant: ItemGrant, parsed_text_sha256: str, parsed_segments: list[dict]
    ) -> None:
        if (
            not isinstance(parsed_text_sha256, str)
            or not _HASH.fullmatch(parsed_text_sha256)
            or type(parsed_segments) is not list
            or not parsed_segments
            or any(
                type(segment) is not dict
                or set(segment) - {"text", "page", "heading"}
                or type(segment.get("text")) is not str
                or not segment["text"].strip()
                or (
                    segment.get("page") is not None
                    and (type(segment["page"]) is not int or segment["page"] < 1)
                )
                or (segment.get("heading") is not None and type(segment["heading"]) is not str)
                for segment in parsed_segments
            )
        ):
            raise ValueError("Parsed snapshot is invalid")
        # Normalize before acquiring locks; no documents are written to logs/errors.
        segments = json.loads(json.dumps(parsed_segments, allow_nan=False))
        async with self.database.session_factory() as session, session.begin():
            selector, operation, item, member, now = await self._item_context(
                session, grant.parent, grant.item_id
            )
            self._item_owned(selector, operation, item, grant, now)
            if member is None:
                member = CoreGenerationRevision(
                    generation_id=item.generation_id,
                    revision_id=item.revision_id,
                    source_id=item.source_id,
                )
                session.add(member)
            if member.state in {"verified", "cleaned"}:
                raise IndexConflict("Audited member cannot be overwritten by an unaudited write")
            if member.claim_token is not None and (
                member.claim_token != grant.claim_token or member.claim_fence != grant.claim_fence
            ):
                raise IndexConflict(
                    "Prior write intent requires strict absence audit before another write"
                )
            if member.claim_token == grant.claim_token and (
                member.parsed_text_sha256 != parsed_text_sha256
                or member.parsed_segments != segments
            ):
                raise IndexConflict("A write attempt cannot change its parsed snapshot")
            member.state = "indexing"
            member.claim_token = grant.claim_token
            member.claim_fence = grant.claim_fence
            member.parsed_text_sha256 = item.parsed_text_sha256 = parsed_text_sha256
            member.parsed_segments = item.parsed_segments = segments
            member.updated_at = item.updated_at = now

    async def record_manifest(self, grant: ItemGrant, chunk_ids: list[str]) -> None:
        chunks = _chunks(chunk_ids)
        async with self.database.session_factory() as session, session.begin():
            selector, operation, item, member, now = await self._item_context(
                session, grant.parent, grant.item_id
            )
            self._item_owned(selector, operation, item, grant, now)
            if (
                member is None
                or member.claim_token != grant.claim_token
                or member.claim_fence != grant.claim_fence
                or member.state != "indexing"
            ):
                raise LeaseLost("A matching write intent is required before recording chunks")
            retained = _chunks(sorted(set(_chunks(member.cleanup_chunk_ids or [])) | set(chunks)))
            member.cleanup_chunk_ids = item.cleanup_chunk_ids = retained
            member.updated_at = item.updated_at = now

    async def fail_item(self, grant: ItemGrant, error_code: str) -> None:
        if error_code not in _ERRORS:
            raise ValueError("Rebuild error code is unsupported")
        async with self.database.session_factory() as session, session.begin():
            selector, operation, item, member, now = await self._item_context(
                session, grant.parent, grant.item_id
            )
            self._item_owned(selector, operation, item, grant, now)
            item.state = "failed"
            item.error = error_code
            item.claim_token = item.lease_owner = item.lease_until = None
            item.updated_at = now
            if (
                member is not None
                and member.claim_token == grant.claim_token
                and member.claim_fence == grant.claim_fence
                and member.state == "indexing"
            ):
                member.state = "failed"
                member.error = error_code
                member.updated_at = now
