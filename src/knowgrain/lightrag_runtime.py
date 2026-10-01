import asyncio
from functools import partial
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from knowgrain.config import Settings

QueryMode = Literal["local", "global", "hybrid", "mix", "naive"]

_READINESS_TIMEOUT_SECONDS = 3.0
_PARTIAL_STORAGE_ATTRIBUTES = (
    "full_docs",
    "text_chunks",
    "full_entities",
    "full_relations",
    "entity_chunks",
    "relation_chunks",
    "entities_vdb",
    "relationships_vdb",
    "chunks_vdb",
    "chunk_entity_relation_graph",
    "llm_response_cache",
    "doc_status",
)


class LightRAGRuntime:
    """Own one embedded LightRAG instance for the lifetime of one event loop."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._rag: Any | None = None
        self._partial_rag: Any | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._model_validation_lock = asyncio.Lock()
        self._model_validation_pending = 0
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._restart_required_detail: str | None = None
        self._model_validation_error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self._rag is not None

    @property
    def restart_required(self) -> bool:
        return self._restart_required_detail is not None

    @property
    def restart_required_detail(self) -> str | None:
        return self._restart_required_detail

    @property
    def model_validation_error(self) -> str | None:
        return self._model_validation_error

    async def start(self) -> None:
        self._bind_event_loop()
        async with self._lifecycle_lock:
            if self.restart_required:
                raise RuntimeError(
                    self._restart_required_detail
                    or "LightRAG requires a Knowgrain process restart before retrying"
                )
            if self._rag is not None:
                return

            await self._start_unlocked()

    async def validate_model_configuration(self) -> None:
        """Check configured Ollama models and measure one actual embedding vector."""
        self._bind_event_loop()
        self._model_validation_pending += 1
        if self._model_validation_error is None:
            self._model_validation_error = "Model configuration validation is in progress"
        succeeded = False
        try:
            async with self._model_validation_lock:
                async with self._ollama_client() as client:
                    models = await self._available_ollama_models(client)
                    missing = [
                        model
                        for model in (self._settings.llm_model, self._settings.embedding_model)
                        if not self._model_is_available(model, models)
                    ]
                    if missing:
                        raise RuntimeError("Ollama model(s) not found: " + ", ".join(missing))

                    response = await client.post(
                        "/api/embed",
                        json={
                            "model": self._settings.embedding_model,
                            "input": ["Knowgrain embedding dimension check"],
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    embeddings = (
                        payload.get("embeddings") if isinstance(payload, dict) else None
                    )
                    vector = embeddings[0] if isinstance(embeddings, list) and embeddings else None
                    if not isinstance(vector, list) or not vector:
                        raise RuntimeError("Ollama returned no embedding vector")
                    actual_dimension = len(vector)
                    if actual_dimension != self._settings.embedding_dim:
                        raise RuntimeError(
                            "Embedding dimension mismatch: "
                            f"configured {self._settings.embedding_dim}, "
                            f"model returned {actual_dimension}"
                        )
            succeeded = True
        except BaseException as exc:
            if isinstance(exc, Exception):
                self._model_validation_error = self._describe_validation_error(exc)
            else:
                self._model_validation_error = (
                    f"Model configuration validation interrupted ({type(exc).__name__})"
                )
            raise
        finally:
            self._model_validation_pending -= 1
            if self._model_validation_pending == 0 and succeeded:
                self._model_validation_error = None
            elif self._model_validation_error is None:
                self._model_validation_error = "Model configuration validation is in progress"

    async def probe_readiness(self) -> dict[str, Any]:
        """Run cheap dependency checks; never perform embedding or LLM inference here."""
        self._bind_event_loop()
        postgres_result, ollama_result = await asyncio.gather(
            self._probe_postgres(), self._probe_ollama_models()
        )
        return {
            "postgres_ready": postgres_result[0],
            "postgres_detail": postgres_result[1],
            "ollama_ready": ollama_result[0],
            "ollama_detail": ollama_result[1],
        }

    async def _start_unlocked(self) -> None:
        postgres_ready, postgres_detail = await self._probe_postgres()
        if not postgres_ready:
            raise RuntimeError(postgres_detail or "PostgreSQL check failed")

        # LightRAG loads its storage configuration while its modules are imported.
        self._settings.configure_lightrag_environment()
        from knowgrain.tokenizer_cache import require_tokenizer_cache

        require_tokenizer_cache(self._settings.tokenizer_cache_dir)
        await self.validate_model_configuration()

        from lightrag import LightRAG
        from lightrag.llm.ollama import ollama_embed, ollama_model_complete
        from lightrag.utils import EmbeddingFunc

        working_dir: Path = self._settings.lightrag_working_dir
        working_dir.mkdir(parents=True, exist_ok=True)

        rag = LightRAG(
            working_dir=str(working_dir),
            workspace=self._settings.lightrag_workspace,
            kv_storage="PGKVStorage",
            vector_storage="PGVectorStorage",
            graph_storage="PGTableGraphStorage",
            doc_status_storage="PGDocStatusStorage",
            tiktoken_model_name="gpt-4o",
            llm_model_func=ollama_model_complete,
            llm_model_name=self._settings.llm_model,
            llm_model_kwargs={
                "host": self._settings.ollama_host,
                "options": {"num_ctx": self._settings.llm_context_size},
            },
            embedding_func=EmbeddingFunc(
                embedding_dim=self._settings.embedding_dim,
                max_token_size=self._settings.embedding_max_token_size,
                func=partial(
                    ollama_embed.func,
                    embed_model=self._settings.embedding_model,
                    host=self._settings.ollama_host,
                ),
            ),
        )
        try:
            await rag.initialize_storages()
        except BaseException as initialization_error:
            self._restart_required_detail = (
                "LightRAG storage initialization failed "
                f"({type(initialization_error).__name__}); restart the Knowgrain process "
                "before retrying, even if best-effort cleanup succeeds"
            )
            cleanup_failures, cleanup_was_cancelled = await self._finalize_partial_storages(rag)
            if cleanup_failures:
                self._partial_rag = rag
                self._restart_required_detail += (
                    "; best-effort storage cleanup failed for: "
                    + ", ".join(cleanup_failures)
                )
            if cleanup_was_cancelled and not isinstance(initialization_error, asyncio.CancelledError):
                raise asyncio.CancelledError from initialization_error
            raise
        self._rag = rag

    async def _finalize_partial_storages(self, rag: Any) -> tuple[list[str], bool]:
        """Finalize the storage fields initialized by LightRAG 1.5.7 one by one."""
        failures: list[str] = []
        found_storage = False
        was_cancelled = False
        for attribute in _PARTIAL_STORAGE_ATTRIBUTES:
            try:
                storage = getattr(rag, attribute)
                if storage is None:
                    continue
                found_storage = True
                finalizer = getattr(storage, "finalize", None)
                if not callable(finalizer):
                    failures.append(f"{attribute} (no finalize method)")
                    continue
                await finalizer()
            except asyncio.CancelledError as exc:
                failures.append(f"{attribute} ({type(exc).__name__})")
                was_cancelled = True
                continue
            except BaseException as exc:
                failures.append(f"{attribute} ({type(exc).__name__})")
                continue

        if not found_storage and not failures:
            failures.append("no initialized storage attributes found")
        if failures:
            self._partial_rag = rag
        else:
            self._partial_rag = None
        return failures, was_cancelled

    async def _probe_postgres(self) -> tuple[bool, str | None]:
        try:
            import asyncpg

            connection = await asyncpg.connect(
                host=self._settings.postgres_host,
                port=self._settings.postgres_port,
                user=self._settings.postgres_user,
                password=self._settings.postgres_password.get_secret_value(),
                database=self._settings.postgres_database,
                timeout=_READINESS_TIMEOUT_SECONDS,
            )
            try:
                result = await connection.fetchval(
                    "SELECT 1", timeout=_READINESS_TIMEOUT_SECONDS
                )
            finally:
                await connection.close()
            if result != 1:
                return False, "PostgreSQL readiness query returned an unexpected result"
            return True, None
        except Exception as exc:
            return False, f"PostgreSQL check failed ({type(exc).__name__}): {exc}"

    async def _probe_ollama_models(self) -> tuple[bool, str | None]:
        try:
            async with self._ollama_client() as client:
                models = await self._available_ollama_models(client)
        except Exception as exc:
            return False, self._describe_validation_error(exc)

        missing = [
            model
            for model in (self._settings.llm_model, self._settings.embedding_model)
            if not self._model_is_available(model, models)
        ]
        if missing:
            return False, "Ollama model(s) not found: " + ", ".join(missing)
        return True, None

    def _ollama_client(self) -> Any:
        import httpx

        return httpx.AsyncClient(
            base_url=self._settings.ollama_host.rstrip("/"),
            timeout=_READINESS_TIMEOUT_SECONDS,
        )

    @staticmethod
    async def _available_ollama_models(client: Any) -> set[str]:
        response = await client.get("/api/tags")
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise RuntimeError("Ollama returned an invalid model list")
        return {
            LightRAGRuntime._normalize_model_name(model_name)
            for model in models
            if isinstance(model, dict)
            for model_name in (model.get("name") or model.get("model"),)
            if isinstance(model_name, str)
        }

    @staticmethod
    def _model_is_available(model_name: str, available_models: set[str]) -> bool:
        return LightRAGRuntime._normalize_model_name(model_name) in available_models

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        # Ollama treats an omitted tag as :latest and may report the expanded form.
        if model_name.rfind(":") <= model_name.rfind("/"):
            return f"{model_name}:latest"
        return model_name

    @staticmethod
    def _describe_validation_error(exc: Exception) -> str:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            return f"Ollama API returned HTTP {exc.response.status_code}"
        if isinstance(exc, httpx.HTTPError):
            return f"Ollama API request failed ({type(exc).__name__})"
        return f"{type(exc).__name__}: {exc}"

    async def close(self) -> None:
        self._bind_event_loop()
        async with self._lifecycle_lock:
            if self._rag is not None:
                rag = self._rag
                try:
                    # Cancelling the queue caller can leave its provider worker
                    # running. End and drain these workers before their cache
                    # storages are finalized.
                    callbacks = getattr(rag, "role_llm_funcs", {})
                    seen = set()
                    for callback in callbacks.values():
                        if id(callback) in seen:
                            continue
                        seen.add(id(callback))
                        shutdown = getattr(callback, "shutdown", None)
                        if callable(shutdown):
                            await shutdown(graceful=False)
                    await rag.finalize_storages()
                except BaseException as close_error:
                    self._restart_required_detail = (
                        "LightRAG storage finalization failed "
                        f"({type(close_error).__name__}); restart the Knowgrain process "
                        "before retrying"
                    )
                    raise
                self._rag = None
                self._partial_rag = None
            elif self._partial_rag is not None:
                cleanup_failures, cleanup_was_cancelled = await self._finalize_partial_storages(
                    self._partial_rag
                )
                if cleanup_was_cancelled:
                    detail = "Could not finalize partial LightRAG storages"
                    if cleanup_failures:
                        detail += ": " + ", ".join(cleanup_failures)
                    raise asyncio.CancelledError(detail)
                if cleanup_failures:
                    raise RuntimeError(
                        "Could not finalize partial LightRAG storages: "
                        + ", ".join(cleanup_failures)
                    )

    async def index_text(
        self,
        *,
        source_id: str,
        text: str,
        file_path: str,
    ) -> None:
        rag = self._require_started()
        await rag.ainsert(text, ids=[source_id], file_paths=[file_path])
        # ainsert returns a tracking ID even if a pipeline stage records failure.
        # Application status must follow the persisted document status, not return alone.
        document = await rag.doc_status.get_by_id_strict(source_id)
        if not document or document.get("status") != "processed":
            document_status = document.get("status", "missing") if document else "missing"
            raise RuntimeError(f"LightRAG document was not processed ({document_status})")

    async def retrieve(self, query: str, *, mode: QueryMode = "mix") -> dict[str, Any]:
        from lightrag import QueryParam

        rag = self._require_started()
        raw = await rag.aquery_data(query, param=QueryParam(mode=mode))
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            return raw
        chunks = raw["data"].get("chunks")
        if not isinstance(chunks, list):
            return raw
        candidates = [
            item for item in chunks[:50]
            if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
            and 0 < len(item["chunk_id"]) <= 256
            and not any(ord(character) < 32 for character in item["chunk_id"])
        ]
        ids = list(dict.fromkeys(item["chunk_id"] for item in candidates))
        records = await rag.text_chunks.get_by_ids(ids) if ids else []
        by_id = dict(zip(ids, records, strict=True))
        verified = []
        for chunk in candidates:
            stored = by_id[chunk["chunk_id"]]
            if not isinstance(stored, dict) or stored.get("content") != chunk.get("content"):
                continue
            try:
                revision_id = UUID(stored["full_doc_id"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            # Core normalizes file_path to a display basename. The stored
            # parent document ID is the revision UUID supplied by index_text.
            verified.append({**chunk, "source_revision_id": str(revision_id)})
        return {**raw, "data": {**raw["data"], "chunks": verified}}

    async def generate_json(self, system_prompt: str, prompt: str) -> str:
        """Use the configured Core model callback after application evidence filtering.

        Retrieval and generation deliberately remain separate: upstream graph
        summaries must not bypass Knowgrain's current-revision provenance checks.
        """
        rag = self._require_started()
        async with asyncio.timeout(240):
            # Core's raw callback lacks hashing_kv/model kwargs. Its query role
            # owns the configured callback, cache binding and concurrency queue.
            result = await rag.role_llm_funcs["query"](
                prompt,
                system_prompt=system_prompt,
                response_format={"type": "json_object"},
                stream=False,
                options={
                    "num_ctx": self._settings.llm_context_size,
                    "num_predict": 4096,
                    "temperature": 0.1,
                },
            )
        if not isinstance(result, str) or not result.strip():
            raise ValueError("Generation model returned no JSON text")
        if len(result.encode("utf-8")) > 128 * 1024:
            raise ValueError("Generation model response exceeds the supported size")
        return result

    def _require_started(self) -> Any:
        self._bind_event_loop()
        if self._rag is None:
            raise RuntimeError("LightRAG Core is not initialized; call await start() first")
        return self._rag

    def _bind_event_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._event_loop is None:
            self._event_loop = loop
        elif self._event_loop is not loop:
            raise RuntimeError("LightRAG Core must be used from its initialization event loop")
