# M3 evidence-backed generation and review contract

Status: implementation contract, not an implemented-feature claim. M2 is accepted locally; M3 remains in progress until actual generation/review/proposal acceptance succeeds.

## Content and model boundaries

- Use the existing embedded LightRAG `retrieve(topic, mode="mix")` in its owning loop. Only chunk content becomes evidence; entity descriptions/relations/reference display numbers cannot be treated as original quotations.
- Resolve returned `data.chunks[].file_path` by exact match against source revision Vault paths. Require source active, `current_revision_id == latest_revision_id == revision.id`, revision ready, parsed-text hash present, and indexed time present. An update awaiting indexing makes its older revision ineligible for new generation. Unknown, ambiguous, historical, deleted and unfinished paths are excluded before the generation model sees any text.
- Verify original bytes SHA-256 in Vault, reparse locally with the existing parser, compare parsed text SHA-256, and find each chunk as an exact substring of parsed text (CRLF may be normalized only consistently with the proven parsed hash). Do not invent offsets, page numbers or headings. Report page/heading only when the excerpt is unambiguously inside one actual parsed segment; multi-segment/ambiguous locations are nullable.
- Evidence identities are deterministic UUIDv5 of revision ID, chunk ID and excerpt hash. Keep `source_id`, `revision_id`, original hash/path, parsed hash, excerpt/hash, parsed-character start/end, available page/heading and indexed time. `m3_types.py` defines immutable contracts.
- `indexed_at` is the first successful index time for the revision's parsed content snapshot. Reindexing the same parsed hash preserves it, so immutable evidence rows/pages remain reusable; index job `updated_at` records each execution's completion. A changed parsed hash changes the snapshot timestamp and requires evidence revalidation.
- Bound retrieval to 50 candidate chunks, 24 accepted evidence items, 6,000 characters per excerpt, 48,000 characters total model evidence and 64 MiB total original reads per collection. Verify full returned chunk before taking a bounded excerpt. Malformed upstream objects are ignored safely or reported without logging content. No eligible evidence means an explicit failure and no Wiki publication.
- The generation LLM returns JSON only: `{title, sections:[{heading, claims:[{key,text,evidence_ids}]}], related_page_ids:[]}`. Title/heading are labels; every factual body paragraph comes from a cited claim. No free uncited summary/body field. All fields are bounded, extra fields rejected, claim keys unique, claims nonempty, evidence IDs from the supplied catalog, related page IDs from supplied existing pages only. Validation checks references, not semantic entailment; human review must show claim and original excerpt side by side.
- Escape model-produced text as literal Markdown content so it cannot inject links, HTML, metadata or code blocks. Render all citation links server-side as `[[Sources/Evidence/{evidence_id}#^ev-{evidence_id}|证据 N]]`. Paths/IDs/status are never chosen by the model. Candidate related-page links use exact resolved Vault paths, and are not automatically created.

## Modules and ownership

| Owner | Files | Responsibility |
| --- | --- | --- |
| Sol | `m3_types.py`, this contract; later generation service/runner/routes/runtime/model callback/file publication | Shared interfaces, integration and lifecycle |
| Luna provenance worker | `provenance.py`, `provenance_repository.py`, corresponding new unit/PostgreSQL tests | Current-revision catalog and byte/parse/quote verification |
| Luna generation contract worker | `generation_contract.py`, corresponding unit tests | Strict JSON validation, prompt and deterministic Wiki/evidence Markdown rendering |
| Luna persistence worker | `models.py` additions, `generation_repository.py`, migration `0005_m3_generation`, corresponding PostgreSQL tests | Durable generation jobs, evidence and claim/page mappings |

Workers must not edit other owners' files, reset the shared tree, alter Core deployment or add dependencies. Read shared contracts before coding; report necessary changes to Sol.

## Provenance interfaces

`ProvenanceRepository(database)`:

- `async eligible_by_paths(paths: Sequence[str]) -> dict[str, EligibleRevision]`: at most 50 unique paths; exact matches only; enforce the complete eligibility predicate above. Do not return the same path for ambiguous records.
- `async eligible_by_ids(ids: Sequence[UUID]) -> dict[UUID, EligibleRevision]`: bounded to 24; same predicate, used before writing/review.

`ProvenanceService(repository, vault)`:

- `async collect(raw: dict) -> tuple[Evidence, ...]`: normalize the pinned Core's response, filter before validating file bytes, execute file parsing off the event loop, cache reads only within this operation. Raise `EvidenceUnavailableError` if none are provable; error messages and logging never contain original text.
- `async validate(evidence: Sequence[Evidence]) -> None`: recheck DB eligibility, revision/original/parsed hashes, exact offsets/excerpt hash and available locations against the current Vault; raise if any cited evidence became stale or changed. A source modified during model inference cannot be published as current.

## Generation contract interfaces

`generation_contract.py` exposes:

- `DraftDocument`, `DraftSection`, `DraftClaim`: Pydantic models (`extra="forbid"`) matching the JSON above; title max 200, heading max 160, 1–8 sections, 1–12 claims per section, at most 48 total claims, text max 1,000, key identifier max 64, 1–6 unique evidence UUID strings per claim, at most 8 related page UUIDs. Reject NUL, control/newline injection in single-line labels and empty/whitespace-only values.
- `parse_draft(response: str, evidence: Sequence[Evidence], related_pages: Sequence[dict]) -> DraftDocument`: response max 128 KiB, JSON object only; may strip one outer JSON code fence. Validate all citation/related identities and duplicate claim keys. Raise a stable `DraftValidationError(ValueError)` with content-safe messages, never raw Pydantic error input.
- `build_generation_prompt(topic, evidence, related_pages) -> tuple[str,str]`: system and user prompts. Untrusted source material is explicitly data; request Chinese JSON, explain exact IDs and no unsupported claims. Related pages sent to model are bounded summaries only.
- `render_draft(document, evidence, related_pages, *, page_id: UUID, job_id: UUID, model: str, generated_at: str, target_page_id: UUID | None = None, target_sha256: str | None = None) -> str`: bounded valid M2 Wiki frontmatter (`kg_status: draft`, `kg_generation_job`, `kg_generator_version: "1"`, optional proposal ID/hash), Markdown claim block anchors and server-created evidence/related links. Output deterministic for these inputs. Source IDs and generation fingerprint in metadata; rich claims remain in durable job/manifest.
- `render_evidence(item: Evidence) -> str`: derived evidence Markdown in `Sources/Evidence/{id}.md`, source/revision/hashes/positions in frontmatter, literal quotation + stable block ID + a relative original-file link. Evidence page is not a managed Wiki identity and does not claim reviewed status.

## Durable database interfaces

Migration `0005_m3_generation` (parent `0004_m2_link_identity`) adds:

- `generation_job`: UUID id, topic, optional target page ID/hash (both or neither), reserved output page UUID, state queued/running/succeeded/failed, phase, attempts, owner/lease, JSON draft/evidence/model result, safe error, optional output hash, timestamps. Job carries no browser-controlled path or credentials. Target FK points to persistent Wiki identity; reserved output ID has no FK until file projection exists.
- `evidence_ref`: fields from `Evidence`; composite source/revision FK enforces matching parent; excerpt hash and source/parsed hashes constrained. Evidence is derived data; originals stay in Vault.
- `generated_page`: page UUID FK, generation job unique FK, draft JSON, generation/output hash, optional proposal target/hash, model metadata, reviewed timestamp; immutable source evidence manifest is rebuildable from retained generation data. External Markdown edits invalidate the generation hash and require a new validated proposal or generation before publish.
- `page_evidence`: page UUID/evidence UUID/claim key composite identity, FKs and no unconstrained text-body copy. All source validity checks use current/latest/ready again at publication transaction boundaries, with source rows locked in stable UUID order.

`GenerationRepository(database)` API:

- `enqueue(topic: str, *, target_page_id: UUID | None = None, expected_target_sha256: str | None = None) -> dict`; returns job_id, output_page_id, state/phase and timestamps.
- `get_job(job_id) -> dict | None`; `list_jobs(*, limit=100, offset=0) -> list[dict]` (1–500).
- `claim(owner: UUID) -> dict | None`: SKIP LOCKED; claims queued or expired running leases, 90-second lease, increments attempts; retained result survives retry.
- `renew(job_id, owner) -> bool`; `release_owner(owner) -> None` (return owned running jobs to queued).
- `store_result(job_id, owner, *, draft: dict, evidence: Sequence[Evidence], model: dict) -> bool`: verify active lease and current evidence, insert immutable evidence refs, retain JSON snapshot before file write. Returns false on lost lease. Source staleness raises `EvidenceUnavailableError` and writes no partial result.
- `complete(job_id, owner, *, page_id: UUID, content_sha256: str, claims: Sequence[dict]) -> bool`: Wiki page must already be projected with reserved ID, matching hash/status draft; verify manifest evidence still current, insert generation/page-evidence records and mark succeeded in one transaction. Claims contain only `key`, `evidence_ids` from retained result; reject mismatch/unknown references.
- `fail(job_id, owner, error: str) -> bool`; `retry(job_id) -> dict` (failed only; preserve already-rendered result and identity); safe length-bound errors.
- `get_generation(page_id) -> dict | None`: includes draft/evidence/model, original generated hash, proposal target/hash, reviewed time. Metadata used by review UI, never provider secrets.
- `record_review(page_id, *, expected_generated_sha256: str, reviewed_sha256: str) -> None`: under a transaction recheck current evidence, reject modified manifest hash, store reviewed time/hash. File transition is coordinated by Sol, not this repository. M5 will add broader reconciliation/backup; this does not permit silent overwrites after crashes.

Persistence worker owns model additions and migration only; Sol will advance readiness after inspecting migration and schema parity. Existing index-job constraint/runner must not be changed.

Migration `0006_m3_lookup_indexes` adds B-tree indexes for exact revision Vault-path resolution and both proposal target foreign keys. Runtime readiness requires this head. Migrations are explicit and additive; ordinary index creation may block writes while upgrading a large existing database.

## Integrated backend subset

- `GenerationService` retrieves/filter/verifies evidence, calls the configured Core query-role model callback, validates structured output and saves its result before exclusive publication. The related-page catalog retains at most eight candidates and 4 KiB of metadata; exact paths are preserved.
- Queued/running generation jobs lock Vault selection. Generation stops before Vault service replacement and before Core shutdown. Core shutdown cancels/drains role queues before finalizing their cache storages.
- Job routes: `POST /api/v1/wiki/drafts`, `GET /api/v1/wiki/generation-jobs`, `GET /api/v1/wiki/generation-jobs/{id}`, `POST /api/v1/wiki/generation-jobs/{id}/retry`. Progress responses omit retained result/excerpts.
- Read routes: `GET /api/v1/wiki/pages/{id}/generation` returns retained claims and current content/evidence flags; `GET /api/v1/evidence/{id}` returns a retained quotation with its current-validity flag and Vault-relative evidence path. Stale quotations remain inspectable but are never silently marked current.
- Repository review recording requires the file to have already been explicitly transitioned and projected as reviewed with the exact reviewed hash. It then revalidates current evidence even on idempotent requests. The file transition/API is still pending.

Backend unit and PostgreSQL checks are not evidence of real-model or Web generation acceptance. See development status for actual verification and remaining work.

## Service flow and remaining user acceptance

The integrated backend supports `POST /api/v1/wiki/drafts` for a topic and optional existing page/hash for a proposal, and generation-job progress/failure/retry routes. Shared same-loop Core retrieves, provenance filters, the model generates structured JSON, references validate, the result persists before exclusive artifact writes, Wiki projection refreshes, then the job completes. Retained-result reuse is covered by service/repository tests; real Core inference and process-restart acceptance remain pending.

Automatic generation always writes a new UUID draft. A reviewed or changed existing page becomes a proposal with target/hash and displayed diff; its body is never automatically replaced. Review endpoint requires explicit user action, page hash match, intact generated manifest and current evidence, then transitions the file to `Wiki/Pages/` with recovery-safe operations. Applying a proposal requires explicit target/proposal hashes; target drift gives 409. Browser shows claims, original quotations, stale evidence, review state and proposal comparison. These flows remain pending until integrated and verified.
