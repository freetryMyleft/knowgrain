"""Knowgrain Wiki endpoints; browsers never select arbitrary file paths."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from knowgrain.wiki_files import WikiConflictError, WikiNotFoundError, WikiValidationError
from knowgrain.wiki_service import WikiScanUnavailableError


class WikiCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(default="", max_length=2 * 1024 * 1024)


class WikiSaveRequest(BaseModel):
    markdown: str = Field(max_length=2 * 1024 * 1024)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def install_wiki_routes(app: FastAPI) -> None:
    async def operation(request: Request, action: Callable[..., Awaitable[dict]]) -> dict:
        runtime = request.app.state.runtime
        # Runtime lock also serializes root changes and prevents a write from
        # finishing in a different Vault from the one it was accepted against.
        async with runtime._runtime_lock:
            if not runtime.database.is_ready or not runtime.vault_ready:
                raise HTTPException(503, "应用数据库或 Vault 未就绪")
            try:
                return await action(runtime.wiki)
            except WikiScanUnavailableError as exc:
                raise HTTPException(503, {
                    "code": "scan_incomplete",
                    "message": "Wiki 扫描不完整；已保留之前的投影，请检查文件数量、路径与权限",
                    "issues": [
                        {"vault_path": issue.vault_path, "code": issue.code, "detail": issue.detail}
                        for issue in exc.scan.issues
                    ],
                }) from exc
            except WikiConflictError as exc:
                current = await runtime.wiki.conflict_current(exc.current)
                raise HTTPException(409, jsonable_encoder({
                    "code": exc.code,
                    "message": "Wiki 文件已变化；请比较当前版本与未保存内容",
                    "current": current,
                    "diff": exc.diff,
                })) from exc
            except WikiNotFoundError as exc:
                raise HTTPException(404, "Wiki 页面不存在或无法唯一识别；请查看扫描问题") from exc
            except WikiValidationError as exc:
                raise HTTPException(422, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(503, "Wiki 文件暂不可读写；请检查 Vault 权限和文件类型") from exc

    @app.get("/api/v1/wiki/pages", tags=["wiki"])
    async def list_pages(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        return await operation(request, lambda service: service.list_pages(limit=limit, offset=offset))

    @app.post("/api/v1/wiki/pages", status_code=201, tags=["wiki"])
    async def create_page(request: Request, payload: WikiCreateRequest, response: Response):
        page = await operation(request, lambda service: service.create(payload.title, payload.body))
        response.headers["Location"] = f"/api/v1/wiki/pages/{page['page_id']}"
        return page

    @app.get("/api/v1/wiki/pages/{page_id}", tags=["wiki"])
    async def get_page(request: Request, page_id: UUID):
        return await operation(request, lambda service: service.get_page(page_id))

    @app.put("/api/v1/wiki/pages/{page_id}", tags=["wiki"])
    async def save_page(request: Request, page_id: UUID, payload: WikiSaveRequest):
        return await operation(request, lambda service: service.save(
            page_id, payload.markdown, payload.expected_sha256,
        ))

    @app.get("/api/v1/wiki/pages/{page_id}/backlinks", tags=["wiki"])
    async def backlinks(request: Request, page_id: UUID):
        return await operation(request, lambda service: service.backlinks(page_id))
