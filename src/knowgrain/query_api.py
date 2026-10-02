"""Knowgrain-owned durable query and evidence download endpoints."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import unicodedata
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from knowgrain.evidence_access import EvidenceFileError
from knowgrain.m3_types import EvidenceUnavailableError
from knowgrain.query_contract import QueryValidationError, validate_question
from knowgrain.query_repository import QueryConflictError


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(strict=True)

    @field_validator("question")
    @classmethod
    def validate_question_field(cls, value: str) -> str:
        return validate_question(value)


def _attachment_header(filename: str, *, fallback: str = "evidence.bin") -> str:
    # The legacy fallback is fixed; all untrusted filename bytes go through
    # RFC 5987 percent encoding and can never become a response header line.
    if not isinstance(filename, str):
        filename = "evidence.bin"
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(
        char
        for char in filename
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
    ).strip(" .")[:240] or "evidence.bin"
    encoded = quote(filename.encode("utf-8", errors="replace"), safe="!#$&+-.^_`|~")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


async def _thread_call_drained(function, *args):
    """Run bounded Vault I/O off-loop and keep cancellation from releasing locks early."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if not task.done():
                continue
            with suppress(Exception, asyncio.CancelledError):
                task.result()
            raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


def install_query_routes(app: FastAPI) -> None:
    async def operation(request: Request, action):
        runtime = request.app.state.runtime
        # Runtime setup may replace Vault/service references. Keep the lock for
        # this bounded DB/provenance/file operation; model work belongs only to
        # the background runner and never runs from an HTTP request.
        async with runtime._runtime_lock:
            if not runtime.database.is_ready or not runtime.vault_ready:
                raise HTTPException(503, "应用数据库或 Vault 未就绪")
            try:
                return await action(runtime)
            except QueryConflictError:
                raise HTTPException(
                    409, {"code": "query_conflict", "message": "问答任务状态已变化，请刷新后重试"}
                ) from None
            except EvidenceUnavailableError:
                raise HTTPException(
                    409, {"code": "stale_evidence", "message": "证据已失效，请核对当前来源"}
                ) from None
            except QueryValidationError:
                raise HTTPException(422, "问答请求不符合问题与引用规则") from None
            except EvidenceFileError as exc:
                _raise_evidence_file_http_error(exc)
            except (SQLAlchemyError, OSError):
                raise HTTPException(503, "问答或证据服务暂不可用，请检查数据库与 Vault") from None

    @app.post("/api/v1/queries", status_code=202, tags=["queries"])
    async def enqueue(request: Request, payload: QueryRequest, response: Response):
        job = await operation(request, lambda runtime: runtime.queries.enqueue(payload.question))
        response.headers["Location"] = f"/api/v1/queries/{job['job_id']}"
        return job

    @app.get("/api/v1/queries", tags=["queries"])
    async def list_jobs(
        request: Request,
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        jobs = await operation(
            request,
            lambda runtime: runtime.queries.list_jobs(limit=limit, offset=offset),
        )
        return {"jobs": jobs}

    @app.get("/api/v1/queries/{job_id}", tags=["queries"])
    async def get_job(request: Request, job_id: UUID):
        job = await operation(request, lambda runtime: runtime.queries.detail(job_id))
        if job is None:
            raise HTTPException(404, "问答任务不存在")
        return job

    @app.post("/api/v1/queries/{job_id}/retry", tags=["queries"])
    async def retry(request: Request, job_id: UUID):
        return await operation(request, lambda runtime: runtime.queries.retry(job_id))

    @app.get("/api/v1/evidence/{evidence_id}/original", tags=["evidence"])
    async def original(request: Request, evidence_id: UUID):
        async def load(runtime):
            evidence = await runtime.generation.repository.get_evidence(evidence_id)
            if evidence is None:
                raise HTTPException(404, "证据不存在")
            content, filename = await _thread_call_drained(
                runtime.queries.evidence_files.original, evidence
            )
            return content, filename

        content, filename = await operation(request, load)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": _attachment_header(filename),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/v1/evidence/{evidence_id}/markdown", tags=["evidence"])
    async def markdown(request: Request, evidence_id: UUID):
        async def load(runtime):
            evidence = await runtime.generation.repository.get_evidence(evidence_id)
            if evidence is None:
                raise HTTPException(404, "证据不存在")
            return await _thread_call_drained(runtime.queries.evidence_files.markdown, evidence)

        content = await operation(request, load)
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": _attachment_header("evidence.md", fallback="evidence.md"),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )


def _raise_evidence_file_http_error(exc: EvidenceFileError) -> None:
    code = getattr(exc, "code", "unavailable")
    if code == "missing":
        raise HTTPException(404, "证据文件不存在") from None
    if code == "conflict":
        raise HTTPException(
            409,
            {"code": "evidence_conflict", "message": "Vault 中的证据文件已被修改"},
        ) from None
    raise HTTPException(503, "证据文件暂不可用，请检查 Vault") from None
