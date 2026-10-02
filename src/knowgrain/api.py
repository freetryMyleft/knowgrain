from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import asyncio
from dataclasses import asdict, dataclass, field
import logging
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from knowgrain.config import Settings
from knowgrain.generation_api import install_generation_routes
from knowgrain.generation_repository import GenerationRepository
from knowgrain.generation_service import GenerationService
from knowgrain.evidence_access import EvidenceAccess
from knowgrain.entity_mapping_api import install_entity_mapping_routes
from knowgrain.entity_mapping_repository import EntityMappingRepository
from knowgrain.entity_mapping_service import EntityMappingService
from knowgrain.query_api import install_query_routes
from knowgrain.query_repository import QueryRepository
from knowgrain.query_service import QueryService
from knowgrain.database import ApplicationDatabase
from knowgrain.job_runner import IndexJobRunner
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.provenance import ProvenanceService
from knowgrain.provenance_repository import ProvenanceRepository
from knowgrain.source_repository import SourceRepository, SourceConflictError, SourceNotFoundError
from knowgrain.source_service import SourceService, InvalidUploadError
from knowgrain.vault import VaultStore
from knowgrain.vault_setup import (
    VaultDatabaseUnavailable,
    VaultPathSetupError,
    VaultSelectionConflict,
    VaultSetupService,
)
from knowgrain.upload_limits import UploadBodyLimitMiddleware
from knowgrain.wiki_api import install_wiki_routes
from knowgrain.wiki_repository import WikiRepository
from knowgrain.wiki_service import WikiService

logger = logging.getLogger(__name__)


class LiveResponse(BaseModel):
    status: str = "ok"
    service: str = "knowgrain"


class ReadyResponse(BaseModel):
    status: str
    lightrag: str
    postgres: str
    ollama: str
    app_database: str
    vault: str
    wiki: str = "unavailable"
    detail: str | None = None


class ImportResponse(BaseModel):
    source_id: UUID
    revision_id: UUID
    job_id: UUID
    duplicate: bool
    vault_path: str


class VaultStatusResponse(BaseModel):
    binding_id: str | None
    root: str
    configured_root: str
    allowed_parent: str
    ready: bool
    selection_enabled: bool
    directories: list[str]
    detail: str | None


class VaultPreviewRequest(BaseModel):
    name: str


class VaultPreviewResponse(BaseModel):
    name: str
    root: str
    exists: bool
    directories: list[str]
    create_directories: list[str]
    selection_allowed: bool
    binding_id: str | None
    expected_root: str


class VaultSelectRequest(BaseModel):
    name: str
    expected_binding_id: UUID | None
    expected_root: str


@dataclass(slots=True)
class ApplicationRuntime:
    """Own application services for one FastAPI event loop."""

    settings: Settings
    lightrag: LightRAGRuntime = field(init=False)
    database: ApplicationDatabase = field(init=False)
    repository: SourceRepository = field(init=False)
    vault: VaultStore = field(init=False)
    vault_setup: VaultSetupService = field(init=False)
    sources: SourceService = field(init=False)
    jobs: IndexJobRunner = field(init=False)
    wiki_repository: WikiRepository = field(init=False)
    wiki: WikiService = field(init=False)
    generation_repository: GenerationRepository = field(init=False)
    provenance_repository: ProvenanceRepository = field(init=False)
    generation: GenerationService = field(init=False)
    query_repository: QueryRepository = field(init=False)
    queries: QueryService = field(init=False)
    entity_mapping: EntityMappingService = field(init=False)
    vault_ready: bool = field(default=False, init=False)
    vault_error: str | None = field(default=None, init=False)
    initialization_error: str | None = field(default=None, init=False)
    _runtime_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.lightrag = LightRAGRuntime(self.settings)
        self.database = ApplicationDatabase(self.settings)
        self.repository = SourceRepository(self.database)
        # Vault paths are resolved only after the app database is ready and the
        # setup service has rejected any symlink components in the chosen path.
        self.vault = VaultStore(Path.cwd())
        self.vault_setup = VaultSetupService(self.settings, self.database)
        self.sources = SourceService(self.settings, self.repository, self.vault)
        self.jobs = IndexJobRunner(self.database, self.repository, self.vault, self.lightrag)
        self.wiki_repository = WikiRepository(self.database)
        self.wiki = WikiService(self.vault, self.wiki_repository)
        self.generation_repository = GenerationRepository(self.database)
        self.provenance_repository = ProvenanceRepository(self.database)
        self.generation = GenerationService(
            self.settings, self.generation_repository,
            ProvenanceService(self.provenance_repository, self.vault), self.lightrag, self.wiki,
        )
        self.query_repository = QueryRepository(self.database)
        self.queries = QueryService(
            self.settings, self.query_repository, self.generation.provenance,
            self.lightrag, EvidenceAccess(self.vault),
        )
        self.entity_mapping = EntityMappingService(
            self.generation, EntityMappingRepository(self.database), self.lightrag, self.wiki,
        )

    async def initialize(self, *, force_model_validation: bool = False) -> bool:
        async with self._runtime_lock:
            database_ready = await self.database.initialize()
            self.vault_ready = False
            self.vault_error = None
            # A retry may replace the Vault object. Stop and release the current
            # runner before setup touches files or installs new service references.
            await self.queries.stop()
            await self.generation.stop()
            await self.jobs.stop()
            await self.wiki.stop()
            try:
                if not database_ready:
                    raise VaultDatabaseUnavailable(
                        self.database.last_error or "Application database is unavailable."
                    )
                new_vault, _ = await self.vault_setup.initialize()
                self._install_vault(new_vault)
            except Exception as exc:
                self.vault_ready = False
                if isinstance(exc, (VaultDatabaseUnavailable, VaultPathSetupError)):
                    self.vault_error = str(exc)
                else:
                    self.vault_error = f"Vault initialization failed ({type(exc).__name__})"
            else:
                self.vault_ready = True
                self.vault_error = None
                self.jobs.start()
                await self._start_wiki()
                self.generation.start()
                await self.queries.start()
            if self.lightrag.restart_required:
                self.initialization_error = (
                    self.lightrag.restart_required_detail
                    or "LightRAG storage initialization failed; restart the Knowgrain process "
                    "before retrying"
                )
                return False

            if self.lightrag.is_ready:
                if force_model_validation:
                    try:
                        await self.lightrag.validate_model_configuration()
                    except Exception as exc:
                        self.initialization_error = self._redact_detail(
                            f"{type(exc).__name__}: {exc}"
                        )
                        logger.warning(
                            "LightRAG model validation failed: %s", self.initialization_error
                        )
                        return False
                self.initialization_error = None
                return True

            try:
                await self.lightrag.start()
            except Exception as exc:
                if self.lightrag.restart_required_detail:
                    detail = self.lightrag.restart_required_detail
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                self.initialization_error = self._redact_detail(detail)
                logger.warning("LightRAG Core is not ready: %s", self.initialization_error)
                return False

            self.initialization_error = None
            return True

    def _install_vault(self, vault: VaultStore) -> None:
        self.vault = vault
        self.sources = SourceService(self.settings, self.repository, vault)
        self.jobs = IndexJobRunner(self.database, self.repository, vault, self.lightrag)
        self.wiki = WikiService(vault, self.wiki_repository)
        self.generation = GenerationService(
            self.settings, self.generation_repository,
            ProvenanceService(self.provenance_repository, vault), self.lightrag, self.wiki,
        )
        self.queries = QueryService(
            self.settings, self.query_repository, self.generation.provenance,
            self.lightrag, EvidenceAccess(vault),
        )
        self.entity_mapping = EntityMappingService(
            self.generation, EntityMappingRepository(self.database), self.lightrag, self.wiki,
        )

    async def _start_wiki(self) -> None:
        try:
            await self.wiki.reconcile()
        except Exception as exc:
            # Import/index remains available when a Wiki scan needs recovery.
            # Wiki requests and periodic scans retry the projection from disk.
            self.wiki.last_error = f"Wiki initialization failed ({type(exc).__name__})"
            logger.warning("%s", self.wiki.last_error)
        self.wiki.start()

    async def vault_status(self) -> dict:
        async with self._runtime_lock:
            return await self.vault_setup.status(ready=self.vault_ready, detail=self.vault_error)

    async def preview_vault(self, name: str) -> dict:
        async with self._runtime_lock:
            return await self.vault_setup.preview(name)

    async def select_vault(
        self,
        *,
        name: str,
        expected_binding_id: UUID | None,
        expected_root: str,
    ) -> dict:
        async with self._runtime_lock:
            candidate, _, source_count = await self.vault_setup.validate_selection(
                name=name,
                expected_binding_id=expected_binding_id,
                expected_root=expected_root,
            )
            self.vault_ready = False
            self.vault_error = "Vault selection is being applied."
            await self.queries.stop()
            await self.generation.stop()
            await self.jobs.stop()
            await self.wiki.stop()
            try:
                new_vault, _ = await self.vault_setup.commit_selection(
                    candidate=candidate,
                    expected_binding_id=expected_binding_id,
                    expected_root=expected_root,
                    source_count=source_count,
                )
                self._install_vault(new_vault)
            except Exception as exc:
                self.vault_ready = False
                if isinstance(exc, (VaultPathSetupError, VaultSelectionConflict)):
                    self.vault_error = str(exc)
                else:
                    self.vault_error = f"Vault selection failed ({type(exc).__name__})"
                raise
            self.vault_ready = True
            self.vault_error = None
            self.jobs.start()
            await self._start_wiki()
            self.generation.start()
            await self.queries.start()
            return await self.vault_setup.status(ready=True, detail=None)

    def _redact_detail(self, detail: str) -> str:
        from urllib.parse import quote, quote_plus

        password_value = self.settings.postgres_password
        password = (
            password_value.get_secret_value()
            if hasattr(password_value, "get_secret_value")
            else str(password_value)
        )
        if password:
            for secret in {password, quote(password, safe=""), quote_plus(password)}:
                if secret:
                    detail = detail.replace(secret, "[redacted]")
        return detail

    async def close(self) -> None:
        try:
            await self.queries.stop()
            await self.generation.stop()
            await self.jobs.stop()
            await self.wiki.stop()
        finally:
            try:
                await self.lightrag.close()
            finally:
                await self.database.close()

    async def readiness(self) -> ReadyResponse:
        probes, application_probe = await asyncio.gather(
            self.lightrag.probe_readiness(), self.database.probe_readiness()
        )
        lightrag_ready = (
            self.lightrag.is_ready
            and not self.lightrag.restart_required
            and self.lightrag.model_validation_error is None
        )
        details = [
            self._redact_detail(detail)
            for detail in (
                self.lightrag.restart_required_detail,
                self.lightrag.model_validation_error,
                self.initialization_error if not self.lightrag.is_ready else None,
                probes["postgres_detail"],
                probes["ollama_detail"],
                application_probe[1],
                self.vault_error,
                self.wiki.last_error,
            )
            if detail
        ]
        is_ready = (
            lightrag_ready and probes["postgres_ready"] and probes["ollama_ready"]
            and application_probe[0] and self.vault_ready and self.wiki.last_error is None
        )
        if self.lightrag.restart_required:
            lightrag_status = "restart_required"
        else:
            lightrag_status = "ready" if lightrag_ready else "unavailable"
        return ReadyResponse(
            status="ready" if is_ready else "not_ready",
            lightrag=lightrag_status,
            postgres="ready" if probes["postgres_ready"] else "unavailable",
            ollama="ready" if probes["ollama_ready"] else "unavailable",
            app_database="ready" if application_probe[0] else "unavailable",
            vault="ready" if self.vault_ready else "unavailable",
            wiki="ready" if self.vault_ready and self.wiki.last_error is None else "unavailable",
            detail="; ".join(details) or None,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    runtime = ApplicationRuntime(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = runtime
        try:
            await runtime.initialize()
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="Knowgrain API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", resolved_settings.api_host],
    )
    app.add_middleware(
        UploadBodyLimitMiddleware,
        max_body_bytes=resolved_settings.max_upload_bytes + 64 * 1024,
    )
    install_wiki_routes(app)
    install_generation_routes(app)
    install_query_routes(app)
    install_entity_mapping_routes(app)

    @app.middleware("http")
    async def validate_write_origin(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if origin is not None and origin.rstrip("/") != expected:
                return JSONResponse(status_code=403, content={"detail": "请求来源不被允许"})
        return await call_next(request)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError):
        return JSONResponse(status_code=503, content={"detail": "应用数据库暂不可用"})

    def source_runtime(request: Request) -> ApplicationRuntime:
        state: ApplicationRuntime = request.app.state.runtime
        if not state.database.is_ready:
            raise HTTPException(503, "应用数据库未就绪；请运行迁移并检查健康状态")
        if not state.vault_ready:
            raise HTTPException(503, "Vault 未就绪；请检查文件目录与权限")
        return state

    async def import_upload(request: Request, file: UploadFile, source_id: UUID | None = None):
        state = source_runtime(request)
        chunks = []
        total = 0
        try:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > resolved_settings.max_upload_bytes:
                    raise HTTPException(413, "文件超过上传大小限制")
                chunks.append(chunk)
            async with state._runtime_lock:
                if not state.database.is_ready:
                    raise HTTPException(503, "应用数据库未就绪；请运行迁移并检查健康状态")
                if not state.vault_ready:
                    raise HTTPException(503, "Vault 未就绪；请检查文件目录与权限")
                result = await state.sources.import_file(
                    file.filename or "", b"".join(chunks), source_id=source_id
                )
        except InvalidUploadError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceNotFoundError as exc:
            raise HTTPException(404, "资料不存在") from exc
        except SourceConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            await file.close()
        return ImportResponse(**asdict(result))

    @app.post("/api/v1/sources", status_code=202, response_model=ImportResponse, tags=["sources"])
    async def upload_source(request: Request, file: UploadFile = File(...)):
        return await import_upload(request, file)

    @app.get(
        "/api/v1/system/vault",
        response_model=VaultStatusResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Application database unavailable"}},
        tags=["system"],
    )
    async def get_vault(request: Request) -> VaultStatusResponse:
        state: ApplicationRuntime = request.app.state.runtime
        try:
            return VaultStatusResponse(**(await state.vault_status()))
        except VaultDatabaseUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post(
        "/api/v1/system/vault/preview",
        response_model=VaultPreviewResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Application database unavailable"}},
        tags=["system"],
    )
    async def preview_vault(
        request: Request, payload: VaultPreviewRequest
    ) -> VaultPreviewResponse:
        state: ApplicationRuntime = request.app.state.runtime
        try:
            return VaultPreviewResponse(**(await state.preview_vault(payload.name)))
        except VaultDatabaseUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except VaultPathSetupError as exc:
            raise HTTPException(422, str(exc)) from exc
        except VaultSelectionConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(
        "/api/v1/system/vault/select",
        response_model=VaultStatusResponse,
        responses={
            status.HTTP_409_CONFLICT: {"description": "Vault selection changed or is locked"},
            status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Vault path is unsafe or not writable"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Application database unavailable"},
        },
        tags=["system"],
    )
    async def select_vault(
        request: Request, payload: VaultSelectRequest
    ) -> VaultStatusResponse:
        state: ApplicationRuntime = request.app.state.runtime
        try:
            result = await state.select_vault(
                name=payload.name,
                expected_binding_id=payload.expected_binding_id,
                expected_root=payload.expected_root,
            )
            return VaultStatusResponse(**result)
        except VaultDatabaseUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except VaultPathSetupError as exc:
            raise HTTPException(422, str(exc)) from exc
        except VaultSelectionConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(
        "/api/v1/sources/{source_id}/revisions",
        status_code=202, response_model=ImportResponse, tags=["sources"],
    )
    async def upload_revision(request: Request, source_id: UUID, file: UploadFile = File(...)):
        return await import_upload(request, file, source_id)

    @app.get("/api/v1/sources", tags=["sources"])
    async def list_sources(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        return await source_runtime(request).repository.list_sources(limit=limit, offset=offset)

    @app.get("/api/v1/sources/{source_id}", tags=["sources"])
    async def get_source(request: Request, source_id: UUID):
        document = await source_runtime(request).repository.get_source(source_id)
        if document is None:
            raise HTTPException(404, "资料不存在")
        return document

    @app.post("/api/v1/sources/{source_id}/reindex", status_code=202, tags=["sources"])
    async def retry_source(request: Request, source_id: UUID):
        try:
            job_id = await source_runtime(request).repository.retry_source(source_id)
        except SourceNotFoundError as exc:
            raise HTTPException(404, "资料不存在") from exc
        except SourceConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job_id": str(job_id)}

    @app.get("/api/v1/jobs/{job_id}", tags=["jobs"])
    async def get_job(request: Request, job_id: UUID):
        job = await source_runtime(request).repository.get_job(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job

    @app.get("/api/v1/health/live", response_model=LiveResponse, tags=["system"])
    async def live() -> LiveResponse:
        return LiveResponse()

    @app.get(
        "/api/v1/health/ready",
        response_model=ReadyResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
        tags=["system"],
    )
    async def ready(request: Request, response: Response) -> ReadyResponse:
        state: ApplicationRuntime = request.app.state.runtime
        readiness = await state.readiness()
        if readiness.status != "ready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness

    @app.post(
        "/api/v1/system/retry-initialize",
        response_model=ReadyResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
        tags=["system"],
    )
    async def retry_initialize(request: Request, response: Response) -> ReadyResponse:
        state: ApplicationRuntime = request.app.state.runtime
        await state.initialize(force_model_validation=True)
        readiness = await state.readiness()
        if readiness.status != "ready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness

    # API routes take precedence; source builds run without a frontend until npm build.
    if resolved_settings.web_dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=resolved_settings.web_dist_dir, html=True), name="web")

    return app
