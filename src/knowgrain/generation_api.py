"""Knowgrain-owned generation endpoints; raw Core/model objects stay server-side."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from knowgrain.generation_repository import GenerationConflictError
from knowgrain.m3_types import EvidenceUnavailableError
from knowgrain.wiki_files import WikiConflictError, WikiNotFoundError, WikiValidationError
from knowgrain.wiki_service import WikiScanUnavailableError


class WikiDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=600)
    target_page_id: UUID | None = None
    expected_target_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def public_job(job: dict) -> dict:
    # Lists/progress never include the retained potentially large source corpus.
    return {key: value for key, value in job.items() if key != "result"}


def install_generation_routes(app: FastAPI) -> None:
    async def operation(request: Request, action: Callable[..., Awaitable]):
        runtime = request.app.state.runtime
        async with runtime._runtime_lock:
            if not runtime.database.is_ready or not runtime.vault_ready:
                raise HTTPException(503, "应用数据库或 Vault 未就绪")
            try:
                return await action(runtime.generation)
            except (GenerationConflictError, WikiConflictError):
                raise HTTPException(409, {"code": "generation_conflict", "message": "生成状态或目标版本已变化，请刷新"}) from None
            except WikiNotFoundError:
                raise HTTPException(404, "Wiki 页面不存在") from None
            except (WikiValidationError, ValueError) as exc:
                if isinstance(exc, EvidenceUnavailableError):
                    raise HTTPException(409, {"code": "stale_evidence", "message": "证据已失效，请核对当前来源"}) from None
                raise HTTPException(422, "生成请求不符合主题、页面或证据规则") from None
            except (WikiScanUnavailableError, SQLAlchemyError, OSError):
                raise HTTPException(503, "生成服务暂不可用，请检查数据库与 Vault") from None

    @app.post("/api/v1/wiki/drafts", status_code=202, tags=["generation"])
    async def enqueue(request: Request, payload: WikiDraftRequest, response: Response):
        job = await operation(request, lambda service: service.enqueue(
            payload.topic, target_page_id=payload.target_page_id,
            expected_target_sha256=payload.expected_target_sha256,
        ))
        response.headers["Location"] = f"/api/v1/wiki/generation-jobs/{job['job_id']}"
        return public_job(job)

    @app.get("/api/v1/wiki/generation-jobs", tags=["generation"])
    async def list_jobs(request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        jobs = await operation(request, lambda service: service.repository.list_jobs(limit=limit, offset=offset))
        return {"jobs": [public_job(job) for job in jobs]}

    @app.get("/api/v1/wiki/generation-jobs/{job_id}", tags=["generation"])
    async def get_job(request: Request, job_id: UUID):
        job = await operation(request, lambda service: service.repository.get_job(job_id))
        if job is None:
            raise HTTPException(404, "生成任务不存在")
        return public_job(job)

    @app.post("/api/v1/wiki/generation-jobs/{job_id}/retry", tags=["generation"])
    async def retry(request: Request, job_id: UUID):
        job = await operation(request, lambda service: service.repository.retry(job_id))
        return public_job(job)

    @app.get("/api/v1/wiki/pages/{page_id}/generation", tags=["generation"])
    async def generation_detail(request: Request, page_id: UUID):
        return await operation(request, lambda service: service.generation_detail(page_id))

    @app.get("/api/v1/evidence/{evidence_id}", tags=["generation"])
    async def evidence_detail(request: Request, evidence_id: UUID):
        return await operation(request, lambda service: service.evidence_detail(evidence_id))
