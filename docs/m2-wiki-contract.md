# M2 Wiki file and API contract

Status: implementation contract, not completed acceptance. M0/M1 evidence remains in `docs/verification/`.

## Authority and scope

Markdown files are authoritative. PostgreSQL stores a rebuildable projection of identities, paths, hashes and links, never the sole copy of a body. M2 supports manual creation/editing; it does not claim model generation or evidence review. New manual pages start as `draft` in `Wiki/Drafts/`.

Managed pages are UTF-8 `.md` regular files recursively beneath `Wiki/Drafts/` or `Wiki/Pages/`, with YAML frontmatter containing UUID `kg_id`, `kg_kind: wiki`, and `kg_status: draft | reviewed`. Scans never add IDs or rewrite existing files. Missing/invalid IDs, duplicate IDs, unsafe paths, oversized files and malformed frontmatter are reported as issues. Duplicate IDs are excluded from the valid projection and cannot be saved. Renames/moves preserve ID. Files outside these directories are not exposed by this API.

Read bounds: 2 MiB per page, 32 KiB frontmatter, 10,000 candidate files, 30,000 directory entries and 128 MiB total bytes per scan; 10,000 links per page and 100,000 links per scan. Reject symlinks, special nodes, NUL metadata and invalid UTF-8; never block on FIFOs. Bound YAML nodes/depth and reject duplicate mapping keys, aliases and custom tags. Use PyYAML SafeLoader rather than object construction. Preserve full Markdown bytes on read and accepted edit; do not regenerate user frontmatter just to refresh the projection. PyYAML and watchfiles are now direct locked dependencies; both already existed transitively in the earlier lock.

## File-layer interface (`wiki_files.py`)

- Frozen `WikiLink(target: str, anchor: str | None, label: str | None, embed: bool, line: int)`.
- Frozen `WikiFile(page_id: UUID, vault_path: str, title: str, status: str, content_sha256: str, markdown: str, links: tuple[WikiLink, ...])`.
- Frozen `WikiIssue(vault_path: str, code: str, detail: str)` and `WikiScan(pages: tuple[WikiFile, ...], issues: tuple[WikiIssue, ...], complete: bool = True)`.
- `parse_wiki(markdown: str, vault_path: str) -> WikiFile`: validate frontmatter; title uses string `title`, then first heading, then filename stem. SHA covers UTF-8 bytes of the entire document. Preserve line endings.
- `parse_wikilinks(markdown: str) -> tuple[WikiLink, ...]`: recognize `[[target#heading|label]]`, block anchors, `![[embed]]`, and local `[[#heading]]`; ignore fenced/inline code, comments and escaped links. No fabricated targets from frontmatter.
- `WikiFileStore(vault: VaultStore)` with synchronous `scan() -> WikiScan`, `create(title: str, body: str) -> WikiFile`, `save(page_id: UUID, markdown: str, expected_sha256: str) -> WikiFile`.
- Creation uses generated UUID filename and exclusive atomic publication; server owns initial frontmatter. Saving resolves ID from a fresh scan, validates unchanged identity/status, checks expected hash immediately before replacement, writes/fsyncs a sibling and uses atomic replace. Web writes serialize in the service. No automatic edit of reviewed body.
- Exceptions: `WikiValidationError(ValueError)`, `WikiNotFoundError(LookupError)`, `WikiConflictError(RuntimeError)` with `current: WikiFile | None`, `code: str`, `diff: str`. Diff compares current Markdown with the submitted Markdown and is bounded to 64 KiB. Duplicate ID, removed page and changed hash never silently choose another file. External replacements after the pre-write check cannot be serialized with an editor that ignores application locks; document this portable-filesystem limit and preserve displaced current bytes in a recovery snapshot before replacing. Do not claim an OS-level compare-and-swap guarantee.

## PostgreSQL projection (`wiki_repository.py`)

Migration `0003_m2_wiki` adds `wiki_page` and `page_link`. IDs use UUID; hashes use lowercase SHA-256 checks. Page fields: `id`, `vault_path`, `title`, `status`, `content_sha256`, `present`, `updated_at`. Link fields: surrogate ID, `from_page_id`, nullable `to_page_id`, `target`, nullable `anchor`/`label`, `embed`, `line`. Foreign keys reference pages. No body column.

`WikiRepository(database)`:

- `async replace_projection(pages: Sequence[WikiFile]) -> None`: one transaction, serialized advisory lock; upsert valid pages in batches of at most 1,000 to stay below asyncpg's parameter bound, mark absent pages `present=False`, replace links as one snapshot. Do not delete user files or persistent identities. Handle swaps/renames without per-row path uniqueness failures. DB paths are not a content authority. An incomplete scan never replaces the prior projection; API returns 503 with diagnostics until traversal is repaired or bounds are met.
- `async list_pages(limit=100, offset=0) -> list[dict]`: present only, deterministic title/path/ID sort; fields match summary below.
- `async get_page(page_id: UUID) -> dict | None`: present only.
- `async backlinks(page_id: UUID) -> list[dict]`: valid present source pages and resolved links only, include link anchor/line.

Resolve links by exact Vault-relative path without `.md`, path relative to the source directory, or unique filename stem/title. Normalize case consistently and reject traversals/absolute targets. Ambiguous/missing targets remain null; never guess. Empty target with anchor resolves to the source page. Raw target is retained for future evidence-page bindings.

Migration `0004_m2_link_identity` widens the link surrogate ID and its sequence to BIGINT without resetting the sequence. DB readiness requires this revision. Both the status selection lock and transaction CAS must count persistent Wiki rows as well as source rows, so creating Wiki first cannot make its Vault switchable. Keep the existing `get_vault_state()` tuple shape, documenting that the second item is now a managed-content count. The filesystem service additionally prevents selecting away from discovered managed Wiki files not yet projected.

## API and lifecycle (Sol integration)

All routes beneath `/api/v1`, same existing origin/host restrictions. Wiki operations need application DB and Vault, not Ollama/LightRAG readiness. Runtime lock prevents concurrent root switch/reinitialization while scanning or writing. File work runs through `asyncio.to_thread`; a service lock serializes scan/projection/write and retains ownership until file work finishes even after repeated request cancellation. Start watcher only after Vault setup, stop before root replacement/shutdown. `watchfiles.awatch` provides events; periodic full scans repair missed events. GET and PUT refresh from disk before using the projection.

| Endpoint | Input | Output |
| --- | --- | --- |
| `GET /wiki/pages` | bounded `limit`, `offset` | `{pages: PageSummary[], issues: WikiIssue[]}` |
| `POST /wiki/pages` | `{title, body}` | 201 `PageDetail`, Location header |
| `GET /wiki/pages/{id}` | UUID | `PageDetail` |
| `PUT /wiki/pages/{id}` | `{markdown, expected_sha256}` | `PageDetail` |
| `GET /wiki/pages/{id}/backlinks` | UUID | `{pages: Backlink[]}` |

Summary: `page_id`, `vault_path`, `title`, `status`, `content_sha256`, `updated_at`. Detail adds `markdown`, `links` with nullable resolved `to_page_id`. Backlink adds source summary plus `target`, `anchor`, `line`, `embed`.

Errors: 404 missing page; 422 invalid Markdown/path/size; 503 unavailable DB/Vault. 409 JSON has `detail: {code, message, current: PageDetail | null, diff}`. Conflict never updates the file; UI retains unsaved text and can reload current content explicitly. No force-overwrite endpoint.

## Acceptance still required

Create/read/save in the real Vault; two pages linking and backlink navigation; Obsidian-compatible files; external rename with same ID; stale Web save after external edit returns 409 with current text/diff and leaves external bytes untouched; duplicate-ID and malformed file diagnostics; watcher/restart reconciliation; actual Web editor and sanitized preview; existing source import unchanged. Unit checks must exercise real filesystem conflicts, code/escape link cases, symlinks/special nodes and resource bounds. PostgreSQL tests use only explicitly selected disposable `knowgrain_test`; do not alter the live acceptance DB until its runtime is cleanly stopped and migration explicitly applied.
