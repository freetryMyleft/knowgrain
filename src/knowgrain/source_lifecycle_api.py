"""Source soft-delete and restore routes."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.exc import SQLAlchemyError

from knowgrain.evidence_access import EvidenceFileError
from knowgrain.source_repository import SourceConflictError, SourceNotFoundError
from knowgrain.source_service import SourceLifecycleUnavailableError


_SOURCE_LIFECYCLE_TIMEOUT_SECONDS = 60


class SourceLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lifecycle_version: StrictInt = Field(ge=0)
    expected_latest_revision_id: UUID | None


def install_source_lifecycle_routes(app: FastAPI) -> None:
    async def operation(request: Request, action):
        runtime = request.app.state.runtime
        async with runtime._runtime_lock:
            if not runtime.database.is_ready or not runtime.vault_ready:
                raise HTTPException(503, "应用数据库或 Vault 未就绪")
            try:
                async with asyncio.timeout(_SOURCE_LIFECYCLE_TIMEOUT_SECONDS):
                    return await action(runtime.sources)
            except SourceNotFoundError:
                raise HTTPException(404, "资料不存在") from None
            except SourceConflictError:
                raise HTTPException(
                    409,
                    {
                        "code": "source_conflict",
                        "message": (
                            "资料状态、版本或 Vault 原件已变化，请刷新并检查后重试"
                        ),
                    },
                ) from None
            except SourceLifecycleUnavailableError as exc:
                raise HTTPException(
                    503,
                    {"code": "source_lifecycle_unavailable", "message": str(exc)},
                ) from None
            except EvidenceFileError:
                raise HTTPException(
                    503,
                    "Vault 原件暂不可安全读取，请检查 Vault 状态与权限后重试",
                ) from None
            except (SQLAlchemyError, OSError, TimeoutError):
                raise HTTPException(
                    503, "来源生命周期服务暂不可用，请检查数据库与 Vault"
                ) from None

    @app.delete("/api/v1/sources/{source_id}", status_code=200, tags=["sources"])
    async def soft_delete_source(
        request: Request,
        source_id: UUID,
        payload: SourceLifecycleRequest = Body(...),
    ):
        return await operation(
            request,
            lambda service: service.soft_delete_source(
                source_id,
                expected_lifecycle_version=payload.expected_lifecycle_version,
                expected_latest_revision_id=payload.expected_latest_revision_id,
            ),
        )

    @app.post("/api/v1/sources/{source_id}/restore", status_code=200, tags=["sources"])
    async def restore_source(
        request: Request,
        source_id: UUID,
        payload: SourceLifecycleRequest = Body(...),
    ):
        return await operation(
            request,
            lambda service: service.restore_source(
                source_id,
                expected_lifecycle_version=payload.expected_lifecycle_version,
                expected_latest_revision_id=payload.expected_latest_revision_id,
            ),
        )
