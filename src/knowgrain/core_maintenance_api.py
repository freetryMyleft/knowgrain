"""Application-owned status and retry routes for persisted Core cleanup jobs."""

from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from knowgrain.source_repository import SourceConflictError, SourceNotFoundError


def install_core_maintenance_routes(app: FastAPI) -> None:
    async def operation(request: Request, action):
        runtime = request.app.state.runtime
        async with runtime._runtime_lock:
            if not runtime.database.is_ready or not runtime.vault_ready:
                raise HTTPException(503, "应用数据库或 Vault 未就绪")
            try:
                return await action(runtime.repository)
            except SourceNotFoundError:
                raise HTTPException(404, "来源或清理任务不存在") from None
            except SourceConflictError:
                raise HTTPException(409, "清理任务或来源周期已变化，请刷新状态后重试") from None
            except SQLAlchemyError:
                raise HTTPException(503, "清理任务服务暂不可用，请检查应用数据库") from None

    @app.get("/api/v1/sources/{source_id}/maintenance", tags=["maintenance"])
    async def list_maintenance(
        request: Request, source_id: UUID, limit: int = Query(default=100, ge=1, le=100)
    ):
        async def action(repository):
            if await repository.get_source(source_id) is None:
                raise SourceNotFoundError()
            return await repository.list_maintenance(source_id, limit=limit)

        return await operation(request, action)

    @app.post("/api/v1/maintenance/{job_id}/retry", status_code=202, tags=["maintenance"])
    async def retry_maintenance(request: Request, job_id: UUID):
        return await operation(request, lambda repository: repository.retry_maintenance(job_id))
