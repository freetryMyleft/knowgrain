import asyncio
import hashlib
from functools import partial
from pathlib import Path
import unicodedata
from typing import Any, Awaitable, Callable, Literal, Sequence
from uuid import UUID

from knowgrain.config import Settings
from knowgrain.index_identity import CoreIndexIdentity
from knowgrain.m3_types import Evidence

QueryMode = Literal["local", "global", "hybrid", "mix", "naive"]

_READINESS_TIMEOUT_SECONDS = 3.0
_MAX_EVIDENCE_ITEMS = 24
_MAX_ENTITY_CANDIDATES = 500
_MAX_MAPPED_ENTITIES = 100
_MAX_ENTITY_CHUNKS = 10_000
_MAX_ENTITY_NAME_LENGTH = 512
_MAX_ENTITY_TYPE_LENGTH = 256
_MAX_ENTITY_CHUNK_ID_LENGTH = 512
_MAX_CLEANUP_CHUNKS = 10_000
_POSTGRES_IDENTIFIER_MAX_LENGTH = 63
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

    def __init__(
        self,
        settings: Settings,
        *,
        index_identity: CoreIndexIdentity | None = None,
    ) -> None:
        self._settings = settings
        if index_identity is not None and not isinstance(index_identity, CoreIndexIdentity):
            raise TypeError("index_identity must be a CoreIndexIdentity")
        self._uses_default_index_identity = index_identity is None
        self._index_identity = index_identity or CoreIndexIdentity.from_settings(settings)
        self._rag: Any | None = None
        self._partial_rag: Any | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._model_validation_lock = asyncio.Lock()
        self._model_validation_pending = 0
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._restart_required_detail: str | None = None
        self._model_validation_error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self._rag is not None

    @property
    def index_identity(self) -> CoreIndexIdentity:
        return self._index_identity

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
        if self._uses_default_index_identity:
            self._validate_legacy_workspace_configuration()

        postgres_ready, postgres_detail = await self._probe_postgres()
        if not postgres_ready:
            raise RuntimeError(postgres_detail or "PostgreSQL check failed")

        # LightRAG loads its storage configuration while its modules are imported.
        self._settings.configure_lightrag_environment(
            workspace=self._index_identity.workspace
        )
        from knowgrain.tokenizer_cache import require_tokenizer_cache

        require_tokenizer_cache(self._settings.tokenizer_cache_dir)
        await self.validate_model_configuration()

        from lightrag import LightRAG
        from lightrag.llm.ollama import ollama_embed, ollama_model_complete
        from lightrag.utils import EmbeddingFunc

        self._validate_vector_table_name_lengths()
        working_dir: Path = self._index_identity.working_dir
        working_dir.mkdir(parents=True, exist_ok=True)

        embedding_options: dict[str, Any] = {}
        if self._index_identity.vector_model_name is not None:
            embedding_options["model_name"] = self._index_identity.vector_model_name

        rag = LightRAG(
            working_dir=str(working_dir),
            workspace=self._index_identity.workspace,
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
                **embedding_options,
                func=partial(
                    ollama_embed.func,
                    embed_model=self._settings.embedding_model,
                    host=self._settings.ollama_host,
                ),
            ),
        )
        try:
            await rag.initialize_storages()
            self._assert_storage_workspaces(rag)
        except BaseException as initialization_error:
            self._restart_required_detail = (
                "LightRAG storage initialization or identity validation failed "
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

    def _validate_legacy_workspace_configuration(self) -> None:
        """Reject a legacy override before pinning the default index identity."""
        # Configure connection values before importing Core: its modules load
        # .env on import. Leave workspace untouched until the upstream resolver
        # has read POSTGRES_WORKSPACE and config.ini using its own precedence.
        self._settings.configure_lightrag_database_environment()
        from lightrag.kg.postgres_impl import ClientManager

        # get_config reads config.ini on each call; no cached client or storage
        # is constructed here. Only inspect workspace, never log the config.
        legacy_workspace = ClientManager.get_config()["workspace"]
        if legacy_workspace and legacy_workspace != self._index_identity.workspace:
            raise ValueError(
                "LIGHTRAG_WORKSPACE conflicts with POSTGRES_WORKSPACE "
                "(or config.ini [postgres] workspace); configure both to the "
                "same existing workspace before starting Knowgrain"
            )

    def _validate_vector_table_name_lengths(self) -> None:
        """Fail before constructing Core if its target vector tables exceed PG limits."""
        model_name = self._index_identity.vector_model_name
        if model_name is None:
            return

        from lightrag.kg.postgres_impl import namespace_to_table_name
        from lightrag.namespace import NameSpace

        suffix = f"{model_name}_{self._settings.embedding_dim}d"
        namespaces = (
            NameSpace.VECTOR_STORE_ENTITIES,
            NameSpace.VECTOR_STORE_RELATIONSHIPS,
            NameSpace.VECTOR_STORE_CHUNKS,
        )
        for namespace in namespaces:
            base_name = namespace_to_table_name(namespace)
            if not isinstance(base_name, str):
                raise RuntimeError(f"LightRAG has no PostgreSQL table for {namespace}")
            table_name = f"{base_name}_{suffix}"
            if len(table_name) > _POSTGRES_IDENTIFIER_MAX_LENGTH:
                raise ValueError(
                    "Embedding dimension makes LightRAG vector table "
                    f"{table_name!r} exceed PostgreSQL's "
                    f"{_POSTGRES_IDENTIFIER_MAX_LENGTH}-character identifier limit"
                )

    def _assert_storage_workspaces(self, rag: Any) -> None:
        """Require every initialized Core storage to retain the selected workspace."""
        missing = object()
        invalid_storages: list[str] = []
        for attribute in _PARTIAL_STORAGE_ATTRIBUTES:
            storage = getattr(rag, attribute, missing)
            if storage is missing:
                invalid_storages.append(f"{attribute} (missing storage)")
                continue
            workspace = getattr(storage, "workspace", missing)
            if workspace is missing:
                invalid_storages.append(f"{attribute} (missing workspace)")
            elif workspace != self._index_identity.workspace:
                invalid_storages.append(f"{attribute} (workspace mismatch)")
        if invalid_storages:
            raise RuntimeError(
                "LightRAG storage identity validation failed for: "
                + ", ".join(invalid_storages)
            )

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
            async with self._write_lock:
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
        before_insert: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        async with self._write_lock:
            rag = self._require_started()
            if before_insert is not None:
                await before_insert()
            await rag.ainsert(text, ids=[source_id], file_paths=[file_path])
            # ainsert returns a tracking ID even if a pipeline stage records failure.
            # Application status must follow the persisted document status, not return alone.
            document = await rag.doc_status.get_by_id_strict(source_id)
            if not document or document.get("status") != "processed":
                document_status = document.get("status", "missing") if document else "missing"
                raise RuntimeError(f"LightRAG document was not processed ({document_status})")

    async def delete_revision(
        self,
        *,
        source_id: str,
        expected_chunk_ids: Sequence[str] | None = None,
        persist_manifest: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
        delete_llm_cache: bool = False,
    ) -> None:
        """Delete one revision through LightRAG's public document API.

        The durable manifest callback runs under the same exclusive write lock
        as indexing and before Core's destructive API. A Core miss only counts
        as an idempotent success after every owned record is positively absent.
        """
        doc_id = self._canonical_revision_id(source_id)
        previous_chunks = self._normalize_cleanup_chunk_ids(expected_chunk_ids)

        async with self._write_lock:
            rag = self._require_started()
            document = await self._strict_get(rag.doc_status, doc_id)
            status_chunks: tuple[str, ...] = ()
            if document is not None:
                if not isinstance(document, dict):
                    raise RuntimeError("LightRAG document status is malformed")
                raw_chunks = document.get("chunks_list")
                if not isinstance(raw_chunks, list):
                    raise RuntimeError("LightRAG document chunk manifest is malformed")
                status_chunks = self._normalize_cleanup_chunk_ids(raw_chunks)

            manifest = tuple(sorted(set(previous_chunks).union(status_chunks)))
            if len(manifest) > _MAX_CLEANUP_CHUNKS:
                raise RuntimeError("LightRAG document cleanup manifest exceeds the supported limit")

            chunk_records = await self._strict_get_many(rag.text_chunks, manifest)
            has_owned_orphan_chunks = False
            for chunk_id, record in zip(manifest, chunk_records, strict=True):
                if record is None:
                    continue
                if not isinstance(record, dict):
                    raise RuntimeError("LightRAG text chunk record is malformed")
                if record.get("full_doc_id") != doc_id:
                    raise RuntimeError("LightRAG cleanup manifest contains a foreign text chunk")
                has_owned_orphan_chunks = True

            if document is None:
                for storage_name in ("full_docs", "full_entities", "full_relations"):
                    record = await self._strict_get(getattr(rag, storage_name, None), doc_id)
                    if record is not None:
                        raise RuntimeError(
                            "LightRAG document status is missing while document data remains"
                        )
                if has_owned_orphan_chunks:
                    raise RuntimeError(
                        "LightRAG document status is missing while owned text chunks remain"
                    )

            if persist_manifest is None and (document is not None or manifest):
                raise RuntimeError(
                    "LightRAG cleanup manifest persistence is required before deletion"
                )
            if persist_manifest is not None:
                await persist_manifest(manifest)

            try:
                raw_result = await self._await_core_deletion(
                    rag.adelete_by_doc_id(doc_id, delete_llm_cache=delete_llm_cache)
                )
            except Exception:
                raise RuntimeError("LightRAG document deletion failed") from None
            result_status = self._normalize_deletion_result(raw_result, doc_id)
            if result_status not in {"success", "not_found"}:
                raise RuntimeError("LightRAG refused or failed to delete the document")

            await self._assert_revision_absent(rag, doc_id, manifest)

    async def inspect_revision(
        self,
        *,
        source_id: str,
        expected_text_sha256: str,
        expected_chunk_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Inspect revision-owned Core records without mutating LightRAG.

        This verifies the persisted document, recovery-anchor, status, and
        text-chunk records using strict point reads. Chunk IDs in the pinned
        Core can be positional or custom, so this checks their stored shape
        and manifest membership without deriving IDs from chunk text. It does
        not inspect the vector or graph stores or prove each chunk body against
        the full document; ``healthy`` is limited to the checked records.
        """
        doc_id = self._canonical_revision_id(source_id)
        if (
            not isinstance(expected_text_sha256, str)
            or len(expected_text_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_text_sha256)
        ):
            raise ValueError("Expected text SHA-256 must be lowercase hexadecimal")
        expected_chunks = self._normalize_inspection_chunk_ids(expected_chunk_ids)

        async with self._write_lock:
            rag = self._require_started()
            try:
                # Read every per-document store even when one is already
                # absent. A miss is meaningful only when strict reads have
                # positively established the whole known revision is absent.
                status = await self._strict_get(rag.doc_status, doc_id)
                full_doc = await self._strict_get(rag.full_docs, doc_id)
                full_entities = await self._strict_get(rag.full_entities, doc_id)
                full_relations = await self._strict_get(rag.full_relations, doc_id)

                if status is None:
                    known_chunks = expected_chunks
                elif isinstance(status, dict) and isinstance(status.get("chunks_list"), list):
                    try:
                        status_chunks = self._normalize_inspection_core_chunk_ids(
                            status["chunks_list"]
                        )
                    except ValueError:
                        status_chunks = ()
                    known_chunks = tuple(sorted(set(status_chunks).union(expected_chunks)))
                else:
                    status_chunks = ()
                    known_chunks = expected_chunks

                chunk_records = await self._inspection_get_many(rag.text_chunks, known_chunks)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Storage exception strings may include SQL, paths, or
                # connection details. Preserve failure semantics without
                # exposing provider or database diagnostics.
                raise RuntimeError("LightRAG revision inspection read failed") from None

            if status is None:
                if any(record is not None for record in (full_doc, full_entities, full_relations)):
                    return self._inspection_result(
                        "inconsistent", (), "orphan_data_without_status"
                    )
                if any(record is not None for record in chunk_records):
                    return self._inspection_result(
                        "inconsistent", (), "orphan_chunks_without_status"
                    )
                return self._inspection_result("missing", (), "revision_missing")

            if not isinstance(status, dict):
                return self._inspection_result("inconsistent", (), "status_invalid")

            raw_status_chunks = status.get("chunks_list")
            try:
                status_chunks = self._normalize_inspection_core_chunk_ids(raw_status_chunks)
            except ValueError:
                return self._inspection_result("inconsistent", (), "chunk_manifest_invalid")
            if status.get("status") != "processed":
                return self._inspection_result(
                    "inconsistent", status_chunks, "status_not_processed"
                )
            if (
                not self._is_nonnegative_int(status.get("chunks_count"))
                or status["chunks_count"] != len(status_chunks)
            ):
                return self._inspection_result(
                    "inconsistent", status_chunks, "chunk_manifest_invalid"
                )

            if full_doc is None or full_entities is None or full_relations is None:
                return self._inspection_result(
                    "inconsistent", status_chunks, "document_record_missing"
                )
            if (
                not isinstance(full_doc, dict)
                or full_doc.get("id") != doc_id
                or not isinstance(full_doc.get("content"), str)
            ):
                return self._inspection_result(
                    "inconsistent", status_chunks, "document_record_invalid"
                )
            try:
                actual_text_sha256 = hashlib.sha256(
                    full_doc["content"].encode("utf-8", errors="strict")
                ).hexdigest()
            except UnicodeEncodeError:
                return self._inspection_result(
                    "inconsistent", status_chunks, "document_content_invalid"
                )
            if actual_text_sha256 != expected_text_sha256:
                return self._inspection_result(
                    "inconsistent", status_chunks, "text_hash_mismatch"
                )
            if full_doc["content"].strip() and not status_chunks:
                return self._inspection_result(
                    "inconsistent", status_chunks, "chunk_manifest_empty"
                )

            if not self._valid_inspection_entity_anchor(full_entities, doc_id):
                return self._inspection_result(
                    "inconsistent", status_chunks, "entity_anchor_invalid"
                )
            if not self._valid_inspection_relation_anchor(full_relations, doc_id):
                return self._inspection_result(
                    "inconsistent", status_chunks, "relation_anchor_invalid"
                )

            if expected_chunks and set(expected_chunks) != set(status_chunks):
                return self._inspection_result(
                    "inconsistent", status_chunks, "chunk_manifest_mismatch"
                )

            records_by_id = dict(zip(known_chunks, chunk_records, strict=True))
            seen_order_indices: set[int] = set()
            for chunk_id in status_chunks:
                record = records_by_id.get(chunk_id)
                if record is None:
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_record_missing"
                    )
                if not isinstance(record, dict):
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_record_invalid"
                    )
                if record.get("full_doc_id") != doc_id:
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_owner_mismatch"
                    )
                if (
                    record.get("id") != chunk_id
                    or not self._valid_entity_chunk_id(record.get("id"))
                    or not isinstance(record.get("content"), str)
                    or not record["content"]
                    or not self._is_nonnegative_int(record.get("chunk_order_index"))
                    or record["chunk_order_index"] in seen_order_indices
                    or not self._inspection_chunk_order_matches_id(
                        doc_id, chunk_id, record.get("chunk_order_index")
                    )
                ):
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_record_invalid"
                    )
                try:
                    record["content"].encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_record_invalid"
                    )
                seen_order_indices.add(record["chunk_order_index"])

            for chunk_id in expected_chunks:
                record = records_by_id.get(chunk_id)
                if record is not None and not isinstance(record, dict):
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_record_invalid"
                    )
                if record is not None and record.get("full_doc_id") != doc_id:
                    return self._inspection_result(
                        "inconsistent", status_chunks, "chunk_owner_mismatch"
                    )

            return self._inspection_result("healthy", status_chunks, None)

    @classmethod
    def _normalize_inspection_chunk_ids(cls, chunk_ids: Sequence[str]) -> tuple[str, ...]:
        if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, Sequence):
            raise ValueError("LightRAG inspection manifest is malformed")
        if len(chunk_ids) > _MAX_CLEANUP_CHUNKS:
            raise RuntimeError("LightRAG inspection manifest exceeds the supported limit")
        normalized: set[str] = set()
        for chunk_id in chunk_ids:
            if not cls._valid_entity_chunk_id(chunk_id):
                raise ValueError("LightRAG inspection manifest contains an invalid chunk ID")
            normalized.add(chunk_id)
            if len(normalized) > _MAX_CLEANUP_CHUNKS:
                raise RuntimeError("LightRAG inspection manifest exceeds the supported limit")
        return tuple(sorted(normalized))

    @classmethod
    def _normalize_inspection_core_chunk_ids(cls, chunk_ids: Any) -> tuple[str, ...]:
        if not isinstance(chunk_ids, list) or len(chunk_ids) > _MAX_CLEANUP_CHUNKS:
            raise ValueError("Core chunk manifest is malformed")
        normalized: list[str] = []
        seen: set[str] = set()
        for chunk_id in chunk_ids:
            if not cls._valid_entity_chunk_id(chunk_id) or chunk_id in seen:
                raise ValueError("Core chunk manifest is malformed")
            seen.add(chunk_id)
            normalized.append(chunk_id)
        return tuple(normalized)

    @staticmethod
    async def _inspection_get_many(storage: Any, keys: tuple[str, ...]) -> list[Any]:
        if not keys:
            return []
        getter = getattr(storage, "get_by_ids", None)
        if not callable(getter):
            raise RuntimeError("LightRAG storage does not support strict bulk reads")
        records = await getter(list(keys))
        if not isinstance(records, list) or len(records) != len(keys):
            raise RuntimeError("LightRAG storage returned an incomplete bulk read")
        return records

    @staticmethod
    def _is_nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _inspection_chunk_order_matches_id(
        doc_id: str, chunk_id: str, order_index: int
    ) -> bool:
        positional_prefix = f"{doc_id}-chunk-"
        if not chunk_id.startswith(positional_prefix):
            return True
        suffix = chunk_id[len(positional_prefix):]
        return not (suffix.isascii() and suffix.isdigit()) or int(suffix) == order_index

    @classmethod
    def _valid_inspection_entity_anchor(cls, record: Any, doc_id: str) -> bool:
        names = record.get("entity_names") if isinstance(record, dict) else None
        count = record.get("count") if isinstance(record, dict) else None
        return (
            isinstance(record, dict)
            and record.get("id") == doc_id
            and isinstance(names, list)
            and all(cls._valid_entity_name(name) for name in names)
            and len(set(names)) == len(names)
            and cls._is_nonnegative_int(count)
            and count == len(names)
        )

    @classmethod
    def _valid_inspection_relation_anchor(cls, record: Any, doc_id: str) -> bool:
        pairs = record.get("relation_pairs") if isinstance(record, dict) else None
        count = record.get("count") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("id") != doc_id
            or not isinstance(pairs, list)
        ):
            return False
        if not cls._is_nonnegative_int(count) or count != len(pairs):
            return False
        normalized: list[tuple[str, str]] = []
        for pair in pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(cls._valid_entity_name(name) for name in pair)
            ):
                return False
            normalized.append((pair[0], pair[1]))
        return len(set(normalized)) == len(normalized)

    @staticmethod
    def _inspection_result(
        state: Literal["healthy", "missing", "inconsistent"],
        chunk_ids: tuple[str, ...],
        reason: str | None,
    ) -> dict[str, Any]:
        return {"state": state, "chunk_ids": chunk_ids, "reason": reason}

    @staticmethod
    def _canonical_revision_id(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Revision ID must be a canonical UUID")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Revision ID must be a canonical UUID") from exc
        if str(parsed) != value:
            raise ValueError("Revision ID must be a canonical UUID")
        return value

    @classmethod
    def _normalize_cleanup_chunk_ids(cls, chunk_ids: Sequence[str] | None) -> tuple[str, ...]:
        if chunk_ids is None:
            return ()
        if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, Sequence):
            raise ValueError("LightRAG cleanup manifest is malformed")
        normalized: set[str] = set()
        for chunk_id in chunk_ids:
            if not cls._valid_entity_chunk_id(chunk_id):
                raise ValueError("LightRAG cleanup manifest contains an invalid chunk ID")
            normalized.add(chunk_id)
            if len(normalized) > _MAX_CLEANUP_CHUNKS:
                raise RuntimeError(
                    "LightRAG document cleanup manifest exceeds the supported limit"
                )
        return tuple(sorted(normalized))

    @staticmethod
    async def _strict_get(storage: Any, key: str) -> Any:
        getter = getattr(storage, "get_by_id_strict", None)
        if not callable(getter):
            raise RuntimeError("LightRAG storage does not support strict point reads")
        return await getter(key)

    @staticmethod
    async def _strict_get_many(storage: Any, keys: tuple[str, ...]) -> list[Any]:
        if not keys:
            return []
        getter = getattr(storage, "get_by_ids", None)
        if not callable(getter):
            raise RuntimeError("LightRAG storage does not support strict bulk reads")
        records = await getter(list(keys))
        if not isinstance(records, list) or len(records) != len(keys):
            raise RuntimeError("LightRAG storage returned an incomplete bulk read")
        for key, record in zip(keys, records, strict=True):
            if record is not None and not isinstance(record, dict):
                raise RuntimeError("LightRAG storage returned a malformed bulk read")
            if isinstance(record, dict) and record.get("id") != key:
                raise RuntimeError("LightRAG storage returned a mismatched bulk read")
        return records

    @staticmethod
    def _normalize_deletion_result(result: Any, doc_id: str) -> str:
        if isinstance(result, dict):
            returned_id = result.get("doc_id")
            status = result.get("status")
        else:
            returned_id = getattr(result, "doc_id", None)
            status = getattr(result, "status", None)
        if (
            not isinstance(returned_id, str)
            or returned_id != doc_id
            or not isinstance(status, str)
            or status not in {"success", "not_found", "not_allowed", "fail"}
        ):
            raise RuntimeError("LightRAG returned a malformed document deletion result")
        return status

    @staticmethod
    async def _await_core_deletion(operation: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(operation)
        cancellation_requested = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if task.done():
                    cancellation_requested = bool(caller and caller.cancelling())
                    result = task.result()
                    break
                if caller is None or not caller.cancelling():
                    raise
                cancellation_requested = True
        if cancellation_requested:
            raise asyncio.CancelledError
        return result

    @classmethod
    async def _assert_revision_absent(
        cls, rag: Any, doc_id: str, chunk_ids: tuple[str, ...]
    ) -> None:
        for storage_name in ("doc_status", "full_docs", "full_entities", "full_relations"):
            record = await cls._strict_get(getattr(rag, storage_name, None), doc_id)
            if record is not None:
                raise RuntimeError("LightRAG document cleanup left document data behind")

        chunk_records = await cls._strict_get_many(rag.text_chunks, chunk_ids)
        if any(record is not None for record in chunk_records):
            raise RuntimeError("LightRAG document cleanup left owned text chunks behind")

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

    async def entities_for_evidence(self, evidence: Sequence[Evidence]) -> dict[str, Any]:
        """Map current evidence chunks to graph entities without using graph summaries.

        Candidate names come from the exact source revisions, then complete
        ``entity_chunks`` membership is intersected with text chunks that were
        independently verified against the evidence quote and revision.
        """
        rag = self._require_started()
        if (
            isinstance(evidence, (str, bytes))
            or not isinstance(evidence, Sequence)
            or len(evidence) > _MAX_EVIDENCE_ITEMS
        ):
            raise ValueError("Evidence list is invalid or exceeds the entity mapping limit")
        if any(not isinstance(item, Evidence) for item in evidence):
            raise ValueError("Evidence list contains an invalid record")
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("Evidence list contains duplicate identities")
        if not evidence:
            return {"entities": [], "truncated": False}

        by_chunk: dict[str, list[Evidence]] = {}
        for item in evidence:
            if (
                not isinstance(item.evidence_id, UUID)
                or not isinstance(item.revision_id, UUID)
                or not self._valid_entity_chunk_id(item.chunk_id)
                or not isinstance(item.excerpt, str)
                or not item.excerpt
            ):
                continue
            by_chunk.setdefault(item.chunk_id, []).append(item)
        if not by_chunk:
            return {"entities": [], "truncated": False}

        text_chunk_ids = sorted(by_chunk)
        text_records = await rag.text_chunks.get_by_ids(text_chunk_ids)
        if not isinstance(text_records, list) or len(text_records) != len(text_chunk_ids):
            return {"entities": [], "truncated": False}

        verified_chunks: dict[str, set[str]] = {}
        revision_ids: set[UUID] = set()
        for chunk_id, record in zip(text_chunk_ids, text_records, strict=True):
            if not isinstance(record, dict):
                continue
            raw_revision_id = record.get("full_doc_id")
            content = record.get("content")
            if not isinstance(raw_revision_id, str) or not isinstance(content, str):
                continue
            try:
                revision_id = UUID(raw_revision_id)
            except (ValueError, AttributeError):
                continue
            citation_ids = {
                str(item.evidence_id)
                for item in by_chunk[chunk_id]
                if item.revision_id == revision_id and content.startswith(item.excerpt)
            }
            if citation_ids:
                verified_chunks[chunk_id] = citation_ids
                revision_ids.add(revision_id)
        if not verified_chunks:
            return {"entities": [], "truncated": False}

        ordered_revisions = sorted(revision_ids, key=str)
        document_records = await rag.full_entities.get_by_ids(
            [str(revision_id) for revision_id in ordered_revisions]
        )
        if not isinstance(document_records, list) or len(document_records) != len(ordered_revisions):
            return {"entities": [], "truncated": False}

        candidate_names: set[str] = set()
        for record in document_records:
            if not isinstance(record, dict):
                continue
            names = record.get("entity_names")
            count = record.get("count")
            if (
                not isinstance(names, list)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != len(names)
            ):
                continue
            candidate_names.update(
                name for name in names if self._valid_entity_name(name)
            )

        all_names = sorted(candidate_names)
        candidates_truncated = len(all_names) > _MAX_ENTITY_CANDIDATES
        selected_names = all_names[:_MAX_ENTITY_CANDIDATES]
        if not selected_names:
            return {"entities": [], "truncated": candidates_truncated}

        entity_chunk_records = await rag.entity_chunks.get_by_ids(selected_names)
        if (
            not isinstance(entity_chunk_records, list)
            or len(entity_chunk_records) != len(selected_names)
        ):
            return {"entities": [], "truncated": candidates_truncated}

        citations_by_name: dict[str, list[str]] = {}
        for name, record in zip(selected_names, entity_chunk_records, strict=True):
            chunk_ids = self._complete_entity_chunk_membership(record)
            if chunk_ids is None:
                continue
            citations = sorted(
                {
                    evidence_id
                    for chunk_id in chunk_ids
                    for evidence_id in verified_chunks.get(chunk_id, ())
                }
            )
            if citations:
                citations_by_name[name] = citations
        if not citations_by_name:
            return {"entities": [], "truncated": candidates_truncated}

        supported_names = sorted(citations_by_name)
        graph_nodes = await rag.chunk_entity_relation_graph.get_nodes_batch(supported_names)
        if not isinstance(graph_nodes, dict):
            return {"entities": [], "truncated": candidates_truncated}

        entities: list[dict[str, Any]] = []
        for name in supported_names:
            node = graph_nodes.get(name)
            if not isinstance(node, dict):
                continue
            node_name = node.get("entity_name")
            if node_name is not None and node_name != name:
                continue
            raw_type = node.get("entity_type", "UNKNOWN")
            entity_type = self._safe_entity_type(raw_type)
            if entity_type is None:
                continue
            entities.append(
                {
                    "entity_id": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                    "name": name,
                    "entity_type": entity_type,
                    "evidence_ids": citations_by_name[name],
                }
            )

        truncated = candidates_truncated or len(entities) > _MAX_MAPPED_ENTITIES
        return {
            "entities": entities[:_MAX_MAPPED_ENTITIES],
            "truncated": truncated,
        }

    async def entity_chunk_ids(self, name: str) -> tuple[str, ...]:
        """Return bounded complete chunk membership for a graph entity candidate."""
        self._validate_entity_name(name)
        rag = self._require_started()
        graph_nodes = await rag.chunk_entity_relation_graph.get_nodes_batch([name])
        if not isinstance(graph_nodes, dict) or not isinstance(graph_nodes.get(name), dict):
            return ()

        record = await rag.entity_chunks.get_by_id(name)
        if record is None:
            return ()
        if not isinstance(record, dict):
            return ()
        chunk_ids = record.get("chunk_ids")
        count = record.get("count")
        if not isinstance(chunk_ids, list):
            return ()
        if isinstance(count, bool) or not isinstance(count, int) or count != len(chunk_ids):
            return ()
        if count > _MAX_ENTITY_CHUNKS:
            raise RuntimeError("Entity chunk membership exceeds the supported limit")
        if any(not self._valid_entity_chunk_id(chunk_id) for chunk_id in chunk_ids):
            return ()
        if len(set(chunk_ids)) != len(chunk_ids):
            return ()
        return tuple(sorted(chunk_ids))

    @classmethod
    def _validate_entity_name(cls, name: str) -> str:
        if not cls._valid_entity_name(name):
            raise ValueError("Entity name must contain 1–512 valid Unicode characters")
        return name

    @staticmethod
    def _valid_entity_name(name: Any) -> bool:
        if not isinstance(name, str) or not 1 <= len(name) <= _MAX_ENTITY_NAME_LENGTH:
            return False
        if not name.strip() or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in name
        ):
            return False
        try:
            name.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return False
        return True

    @staticmethod
    def _valid_entity_chunk_id(chunk_id: Any) -> bool:
        if not isinstance(chunk_id, str) or not 1 <= len(chunk_id) <= _MAX_ENTITY_CHUNK_ID_LENGTH:
            return False
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in chunk_id
        ):
            return False
        try:
            chunk_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return False
        return True

    @staticmethod
    def _complete_entity_chunk_membership(record: Any) -> list[str] | None:
        if not isinstance(record, dict):
            return None
        chunk_ids = record.get("chunk_ids")
        count = record.get("count")
        if (
            not isinstance(chunk_ids, list)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count != len(chunk_ids)
        ):
            return None
        if count > _MAX_ENTITY_CHUNKS:
            raise RuntimeError("Entity chunk membership exceeds the supported limit")
        if any(not LightRAGRuntime._valid_entity_chunk_id(item) for item in chunk_ids):
            return None
        if len(set(chunk_ids)) != len(chunk_ids):
            return None
        return chunk_ids

    @staticmethod
    def _safe_entity_type(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value:
            return "UNKNOWN"
        if len(value) > _MAX_ENTITY_TYPE_LENGTH or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        ):
            return None
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        return value

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
