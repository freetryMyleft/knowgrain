# M4 questions and evidence navigation

This contract defines the current implementation node. M4 also requires Wiki/Core entity mappings, implemented and independently verified in the subsequent [entity node](m4-entity-mapping-contract.md); the question node alone does not establish that result.

## HTTP and UI

- `POST /api/v1/queries` accepts only `{question: string}` (trimmed 1–1000 characters), creates a durable job and returns 202. No client-supplied citations or filesystem paths.
- `GET /api/v1/queries?limit=100&offset=0` returns `{jobs: [...]}` without retained result/corpus.
- `GET /api/v1/queries/{job_id}` returns job state and, on completion, its retained result. States: queued, running, succeeded, failed. Result freshness is recalculated on each read.
- `POST /api/v1/queries/{job_id}/retry` retries failed jobs only. Expired leases are reclaimable. Incomplete work regenerates; only successful immutable results are retained.
- `GET /api/v1/evidence/{evidence_id}` remains the shared M3 evidence viewer: exact quote, source/revision/hash/offset/index time and current flag.
- `GET /api/v1/evidence/{evidence_id}/original` returns hash-verified original bytes as an attachment. A known retained evidence ID resolves the exact historical revision; it cannot silently redirect to the latest file. Missing/damaged/symlinked originals fail closed. Historical evidence is visible with a stale label, but never enters a new answer.
- `GET /api/v1/evidence/{evidence_id}/markdown` returns the ordinary Vault evidence Markdown only when its bytes match the canonical retained evidence rendering; external changes are reported as conflicts.

Answer model JSON is `{status: "answered" | "insufficient", claims: [{key, text, evidence_ids}]}`. Each answered claim has 1–6 unique known evidence UUIDs; 1–12 claims, unique keys, plain bounded text. Insufficient has no claims. No uncited narrative is rendered. Model text is displayed as ordinary React text, without interpreting HTML or automatically creating links; legitimate facts containing a URL remain representable. The server uses the fixed message `无法核实：当前资料不足以支持该问题。` for insufficient evidence. Unknown citations or malformed JSON fail the job rather than fabricating an answer. The prompt treats questions and quoted documents as untrusted data.

Successful result: `{status, message, claims, evidence, model, evidence_current}`. `model` has `name`, `provider`, `generated_at`. Evidence uses the existing M3 snapshot fields plus `current` in public reads. Only cited evidence is retained; insufficient results retain no evidence. Claims and quotes are separate so users can inspect support. Semantic entailment still requires M6 evaluation; valid citation IDs alone do not prove that a model's interpretation is correct.

Web questions poll the durable job, expose retry, restore saved results after navigation/reload, and suppress late selection updates. Evidence navigation displays current/stale state, exact revision/hash/position/indexed time and original/Markdown download controls. Wiki evidence links use the same stable evidence ID rather than a filesystem path supplied by the browser.

## Backend and persistence

`QueryService(settings, repository, provenance, lightrag, evidence_files)` owns a single asyncio job runner and uses the existing embedded Core on the owning loop. It retrieves structured chunks with a 240-second timeout, calls `ProvenanceService.collect`, sends only verified quotes to the configured query-role model, strictly validates its JSON, revalidates cited evidence, publishes exclusive evidence Markdown and completes a durable query record. Empty/stale retrieval returns the explicit insufficient result without asking the model to invent facts. Stale evidence at final validation likewise becomes insufficient; infrastructure/model failures remain retryable failed jobs.

`QueryRepository` methods: enqueue(question), list_jobs(limit, offset), get_job(id), claim_next(owner, lease_seconds=90), renew(id, owner, lease_seconds=90), complete(id, owner, result, evidence), fail(id, owner, error), retry(id). Snapshots use `job_id, question, state, attempts, error, created_at, updated_at, lease_until, result`. Completion locks source/revision rows and checks the same active/current/latest/ready/hash/index-time predicate as generation, inserts immutable shared evidence rows, and commits the result atomically. Lease decisions use database time after lock waits and before final writes. Read-only history is never presented as current without fresh verification.

Migration `0008_m4_queries` adds `query_job` with bounded question, state, attempts, owner/lease, result JSONB, safe error and timestamps. No original documents or Wiki bodies are duplicated. Existing `evidence_ref` is reused, with exact immutable equality checks. This was the question node schema head; the subsequent entity node adds `0009_m4_entity_lookup`, now required by readiness. Migrations are applied explicitly, never by startup.

`EvidenceAccess(vault)` owns exclusive canonical evidence publication and bounded original/Markdown reading. Reads reject path escapes and symlinks, compare full hashes and file identity before/after reading, and return captured verified bytes rather than reopening a path after verification.

## Ownership for this node

- Sol: this contract, ApplicationRuntime/API wiring and final integration/docs/acceptance.
- Luna persistence: models.py (add QueryJob only), database.py schema head, 0008 migration, query_repository.py, evidence_access.py and focused persistence/file tests.
- Luna service: query_contract.py, query_service.py, query_api.py and focused contract/service/API tests.
- Luna frontend: QuestionsWorkspace.tsx, EvidencePanel.tsx, question-contract.ts, question.css, WikiWorkspace.tsx/WikiGenerationPanel.tsx evidence navigation and focused frontend checks. App.tsx navigation remains Sol-owned.

Acceptance requires actual Web → durable job → local qwen3.6:35b → verified citations → exact original, plus stale/no-evidence failure checks. Mocked model tests do not establish real model quality. Subsequent graph/page binding acceptance is recorded separately in the entity-node verification.
