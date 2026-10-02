"""Read-only Wiki/entity navigation routes owned by Knowgrain."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from knowgrain.entity_mapping_service import (
    EntityMappingNotFoundError,
    EntityMappingUnavailableError,
    EntityMappingValidationError,
)
from knowgrain.wiki_service import WikiScanUnavailableError

_ENTITY_MAPPING_TIMEOUT_SECONDS = 60


def install_entity_mapping_routes(app: FastAPI) -> None:
    async def operation(request: Request, action):
        runtime = request.app.state.runtime
        async with runtime._runtime_lock:
            if not runtime.database.is_ready or not runtime.vault_ready:
                raise HTTPException(503, "应用数据库或 Vault 未就绪")
            try:
                async with asyncio.timeout(_ENTITY_MAPPING_TIMEOUT_SECONDS):
                    return await action(runtime.entity_mapping)
            except EntityMappingNotFoundError:
                raise HTTPException(404, "Wiki 页面不存在") from None
            except EntityMappingValidationError:
                raise HTTPException(422, "实体名称不符合检索规则") from None
            except (EntityMappingUnavailableError, WikiScanUnavailableError, TimeoutError):
                raise HTTPException(503, "实体映射暂不可用，请检查 Core、数据库与 Vault") from None
            except (SQLAlchemyError, OSError):
                raise HTTPException(503, "实体映射暂不可用，请检查 Core、数据库与 Vault") from None

    @app.get("/api/v1/wiki/pages/{page_id}/entities", tags=["entity-mapping"])
    async def page_entities(request: Request, page_id: str):
        return await operation(
            request,
            lambda service: service.page_entities(page_id),
        )

    @app.get("/api/v1/graph/entity-pages", tags=["entity-mapping"])
    async def entity_pages(
        request: Request,
        name: str = Query(..., min_length=1, max_length=512),
    ):
        return await operation(
            request,
            lambda service: service.entity_pages(name),
        )
