# M1 implementation contract

This batch prepares the source-import slice while M0's real PostgreSQL/Ollama proof remains pending. It does not declare M0 or M1 accepted. Read AGENTS.md and the full architecture before implementing.

## Shared interfaces

Root owns `config.py`, `api.py`, `source_service.py`, `job_runner.py`, README, Makefile, and integration documentation. Workers must not edit those files or revert others' changes.

Settings gains `knowgrain_postgres_db: str = "knowgrain"`, `vault_root: Path = Path("./data/vault")`, `max_upload_bytes: int = 20 * 1024 * 1024`. Application DB uses the same host/port/user/password and the separate `knowgrain_postgres_db` database. App migrations run explicitly, never implicitly create/drop tables during API startup.

### Vault and parsers worker

Own `src/knowgrain/vault.py`, `src/knowgrain/parsers.py`, `tests/test_vault.py`, `tests/test_parsers.py` only.

`VaultStore(root: Path)` exposes `initialize()`, `resolve(relative_path: str) -> Path`, `write_source(source_id: UUID, revision_id: UUID, filename: str, content: bytes) -> str`, `read_bytes(relative_path: str) -> bytes`. Synchronous APIs are called with `asyncio.to_thread` by the service. Source paths are `Sources/Files/{source_id}/{revision_id}{normalized_suffix}`. Original filenames remain database metadata. Reject absolute/traversal paths and symlink components. Do not overwrite original revisions: publish atomically without clobbering; identical existing content is idempotent, different content raises. Preserve .obsidian and existing files. Initialize only the intended Vault directories.

`parse_document(filename: str, content: bytes) -> ParsedDocument`; `ParsedDocument` has `text: str`, `segments: list[ParsedSegment]`, `parser_version: str = "1"`; `ParsedSegment` has `text: str`, `page: int | None = None`, `heading: str | None = None`. Types are dataclasses so `dataclasses.asdict` works. Support UTF-8 MD/Markdown/TXT, text PDF (reliable page numbers), DOCX paragraphs/tables. Reject unsupported, undecodable, encrypted, empty/scanned-only documents with `DocumentParseError(ValueError)`. Impose a reasonable decompression bound for DOCX. Do not claim PDF chunk-level locations before mapping is implemented.

Use meaningful standard-library unittest cases for immutable source writes, traversal/symlinks, duplicate bytes, UTF-8 failure, unsupported/empty input, PDF blank page handling, DOCX extraction. Do not install new libraries; pypdf and python-docx are already dependencies.

### Database worker

Own `src/knowgrain/models.py`, `src/knowgrain/database.py`, `src/knowgrain/source_repository.py`, `alembic.ini`, `infra/migrations/` only.

SQLAlchemy models and a reviewed initial Alembic migration: `source_document` (id, filename, active/deleted state, latest_revision_id, current_revision_id, created_at), `source_revision` (id, source_id, sha256, vault_path, media_type, index_state, parsed_text_sha256, parsed_segments JSONB, error, created_at, indexed_at), `job` (id, kind index, revision_id, state, attempts, lease_owner, lease_until, error, created_at, updated_at). Use UUID, timezone-aware timestamps, constraints and indexes. One revision per source+sha256, one index job per revision. Current and latest revision references must belong to the same source, either enforced in schema or transaction code. No source original bytes in DB.

`ApplicationDatabase(settings)` owns async SQLAlchemy engine and `session_factory`; `async initialize()` probes SELECT 1 and verifies the schema is migrated (no automatic schema creation); `async close()` disposes engine.

`SourceRepository(database)` methods:

- `async register_source(filename: str, sha256: str, media_type: str, persist: Callable[[UUID, UUID], Awaitable[str]], source_id: UUID | None = None) -> ImportResult`. Dataclass result fields `source_id`, `revision_id`, `job_id` UUID, `duplicate: bool`, `vault_path: str`. Transaction + PostgreSQL advisory lock/row lock for concurrency. New uploads deduplicate against active sources by content hash. Explicit source updates deduplicate within that source. Persist immutable original before committing rows; on failure roll back DB, retain original for reconciliation. New revision sets latest pointer; current pointer changes only after successful index. Define `SourceNotFoundError` and `SourceConflictError` in repository.
- `async list_sources() -> list[dict]`, `get_source(source_id) -> dict | None`: include source id, filename, latest/current revision ids, revision status/hash/vault path/error, created_at. JSON-ready values (UUID strings, ISO dates).
- `async get_revision(revision_id) -> dict | None`: source_id, filename, vault_path, sha256, media_type, state at minimum.
- `async get_job(job_id) -> dict | None`.
- `async retry_source(source_id) -> UUID`: explicitly queue the latest revision's job; reject already queued/running with conflict. Preserve original.
- `async claim_job(owner: UUID) -> dict | None`: `FOR UPDATE SKIP LOCKED`; queued or expired-running jobs; assign 90-second lease, increment attempts, revision indexing; include job_id, revision_id, source_id, filename, vault_path, sha256.
- `async renew_lease(job_id, owner) -> bool`: only running job of owner; extend 90s.
- `async complete_job(job_id, owner, text_sha256: str, segments: list[dict]) -> bool`: conditional lease ownership; revision ready, job succeeded; advance current pointer only if this revision is still source.latest_revision_id and source active. Older in-flight completion cannot replace newer current revision.
- `async fail_job(job_id, owner, error: str) -> bool`: conditional owner; revision/job failed; bounded error text.
- `async release_owner(owner) -> None`: requeue owned running jobs on graceful shutdown.

Alembic uses settings and separate app DB; offline SQL generation must work. No commits, destructive service commands, or global host configuration. Validate import/compilation, PostgreSQL DDL/offline migration generation. Real transaction tests await a real PostgreSQL instance; do not substitute SQLite to claim PostgreSQL semantics.

## Root integration acceptance

POST /api/v1/sources multipart creates source + original + queued job, returns 202; GET /sources and /jobs/{id}; POST /sources/{id}/revisions; POST /sources/{id}/reindex queues explicit retry. Validate size, extension and UUID; never accept arbitrary absolute file paths. API returns 503 when app DB is unavailable. Job runner is one task in the same event loop as LightRAG, polls repository, reads original from Vault, verifies raw hash, parses in thread, calls index_text with revision UUID and Vault path, marks success only after verifying LightRAG doc status processed. Use lease renewal during indexing and cancel/await in-flight work before LightRAG teardown. Persistent originals remain after parsing/model failure.
