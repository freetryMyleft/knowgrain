"""Transactional generation fencing; not yet wired to production workers.

Callers acquire this fence before source/task locks and validate their complete
execution grant with a fresh database clock after their final lock acquisition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowgrain.models import CoreSelector, RebuildOperation


class IndexConflict(RuntimeError):
    """A safe conflict diagnostic, without model settings or document content."""


class LeaseLost(IndexConflict):
    """The full execution ownership no longer authorizes a mutation."""


@dataclass(frozen=True, slots=True)
class ExecutionGrant:
    task_id: UUID
    generation_id: UUID
    execution_epoch: int
    owner: UUID
    claim_token: UUID
    claim_fence: int


@dataclass(frozen=True, slots=True)
class OperationGrant:
    operation_id: UUID
    generation_id: UUID
    owner: UUID
    claim_token: UUID
    claim_fence: int


@dataclass(frozen=True, slots=True)
class ItemGrant:
    item_id: UUID
    source_id: UUID
    revision_id: UUID
    parent: OperationGrant
    execution_epoch: int
    snapshot_sha256: str
    owner: UUID
    claim_token: UUID
    claim_fence: int


class QuiescenceGuard(ABC):
    """Future coordinator witness, held until snapshot commit.

    Its implementation must keep admission closed and check actual Core, model,
    file and review execution has drained. Database lease expiry is insufficient.
    No production implementation or public receipt input exists in this node.
    """

    @abstractmethod
    def assert_quiescent(
        self, operation_id: UUID, old_generation_id: UUID | None, execution_epoch: int
    ) -> None:
        """Raise IndexConflict unless this exact coordinator scope remains drained."""


async def database_now(session: AsyncSession) -> datetime:
    now = await session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise IndexConflict("Database clock is unavailable")
    return now


async def lock_index_fence(
    session: AsyncSession,
    *,
    purpose: Literal["admit", "drain"],
    generation_id: UUID | None = None,
    execution_epoch: int | None = None,
) -> CoreSelector:
    """Lock first in the caller's transaction; never infer a missing selector."""
    if not session.in_transaction() or purpose not in {"admit", "drain"}:
        raise ValueError("Index fence requires a transaction and supported purpose")
    selector = await session.scalar(
        select(CoreSelector).where(CoreSelector.id == 1).with_for_update(read=True)
    )
    if selector is None:
        raise IndexConflict("Index selector has not been initialized")
    if selector.restart_required:
        raise IndexConflict("Index coordinator requires a process restart")
    if generation_id is not None and selector.active_generation_id != generation_id:
        raise LeaseLost("Execution generation is no longer active")
    if execution_epoch is not None and selector.execution_epoch != execution_epoch:
        raise LeaseLost("Execution epoch is no longer active")
    if purpose == "drain" and (generation_id is None or execution_epoch is None):
        raise ValueError("Drain requires the admitted generation and execution epoch")
    if selector.frozen:
        if purpose == "admit":
            raise IndexConflict("Index admission is frozen")
        operation = await session.scalar(
            select(RebuildOperation).where(RebuildOperation.id == selector.pending_rebuild_id)
        )
        if (
            operation is None
            or operation.state not in {"queued", "preparing"}
            or operation.snapshot_sha256 is not None
        ):
            raise LeaseLost("Preparing-stage drain is no longer permitted")
    return selector
