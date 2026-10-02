# M3 explicit review and proposal application

Status: implementation contract; not an acceptance claim. This extends m3-generation-contract.md without changing automatic generation's exclusive-new-draft policy.

## Interfaces and ownership

- Sol: generation_service.py, generation_api.py, api.py/database.py readiness, integration tests/docs.
- Luna files: new review_files.py + tests/test_review_files.py ONLY.
- Luna persistence: models.py additions, new review_repository.py, migration 0007_m3_review, tests/test_review_repository_postgres.py ONLY.
- Luna Web: WikiWorkspace.tsx, new WikiGenerationPanel.tsx/generation-contract.ts/generation.css, frontend contract tests ONLY. Do not change existing wiki-contract.ts/API.

Workers share the directory; do not revert other edits. No new dependencies.

## File operation

`ReviewFileStore(WikiFileStore).commit(operation_id: UUID, page_id: UUID, expected_sha256: str, markdown: str) -> WikiFile`:

- Called only under WikiService lock after explicit action, provenance and DB preparation. New Markdown must match page ID and status reviewed. Operates on known managed Wiki identities only.
- Preserve an existing Wiki/Pages path; otherwise destination Wiki/Pages/{page_id}.md. Never replace a different ID at that destination. Input Markdown chosen/rendered by Sol, not model/browser.
- Persist immutable intent under `.knowgrain/review-operations/{operation_id}/intent.json` before changing files: old path/hash/bytes and new path/hash/bytes/page ID. Bounds and Vault path/symlink rules apply to journal too. Use exclusive publication and fsync; existing intent must match exactly.
- Save recovery of old bytes using existing helpers. Same-path updates check old hash immediately before atomic replace; moved updates publish destination exclusively, then remove old file only if its hash/identity still matches old intent. Never unlink or overwrite externally changed bytes.
- Retry recognizes old-only, new-only and exact old+new partial states from its own journal. Complete exact partial moves; refuse ambiguous duplicates/changed identities. Retry after completed file write returns exact new file without reapplying. The intent is retained after DB completion for crash diagnostics; no silent restoration/overwrite.
- Do not remove arbitrary files. Cross-process editor check-to-replace/unlink race remains the documented portable M2 limitation. Interrupted states preserve content, and retry is an explicit recovery action.
- Test real files: review move, same-path apply, idempotence, interruption after exclusive destination publication, external old/new edits, duplicate IDs/symlinks/journal mismatch. No DB in this layer.

## Database operation

Add `review_operation`: operation_id PK UUID; page_id FK wiki_page; generation_page_id FK generated_page; expected_page_sha256, expected_generation_sha256, reviewed_sha256; state prepared/completed; created_at/completed_at. SHA/check/state constraints, target and generation indexes. No Markdown bodies/secrets in DB.

Add `page_generation_binding`: page_id PK FK wiki_page; generation_page_id FK generated_page; reviewed_sha256; reviewed_at; operation_id FK review_operation. Index generation_page_id. This maps a current reviewed target to an immutable generated manifest; proposal draft and its own manifest stay intact. Applying a proposal does not duplicate/delete its generation job or overwrite its draft. M4 reads evidence via this binding.

`ReviewRepository(database)`:

- `prepare(operation_id, *, page_id, generation_page_id, expected_page_sha256, expected_generation_sha256, reviewed_sha256) -> dict`: immutable/idempotent fields. Lock page/manifest rows in stable order and recheck active/current/latest/ready evidence via existing GenerationRepository helpers. For same-ID review require expected_page==generated hash. For proposal require manifest target ID/hash matches supplied target and proposal projected hash is its original generated hash. Fresh request requires target present and exact expected hash; repeat prepared request permits old hash or exact reviewed output. Reject any other hash/status. Store prepared record atomically. Existing completed request still validates evidence and matching reviewed projection. No file writes.
- `complete(operation_id) -> dict`: lock operation, page/manifest; require present reviewed projection at reviewed_sha256 and current evidence again. Upsert current binding and mark completed atomically. Idempotent completed request verifies matching current binding/hash; never reverts newer bindings. For same-ID review also set generated_page.reviewed_at/hash consistently; proposal manifest stays unchanged.
- `get_binding(page_id) -> dict | None`: generation_page_id, reviewed_sha256, reviewed_at, operation_id.
- Safe errors: GenerationConflictError or EvidenceUnavailableError, no raw source logging. Tests use only disposable knowgrain_test and fixture UUID cleanup. Migration additive, Sol advances readiness after reviewing/applies.

## Sol service/API and Web

Existing generation detail adds `generation_page_id` and `proposal`:

`proposal: null | {target_page_id, target_title, target_sha256, expected_target_sha256, target_changed: bool, diff: str}`. Diff compares target/proposal Markdown bodies only, is bounded to 64 KiB and escaped by UI. It must not suggest copying a proposal's identity or draft status onto the target. Existing draft/evidence/model/current flags remain. For bound reviewed targets, manifest comes from generation_page_id and expected current hash is binding.reviewed_sha256; public proposal metadata is suppressed because this is the reviewed target, not an unapplied proposal.

- `POST /api/v1/wiki/pages/{page_id}/review` body `{expected_sha256}` -> PageDetail. Require untouched generated draft/current evidence; no browser Markdown or status. Set reviewed in server frontmatter, persist operation before file write, update projection and complete binding. Hash/manifest/source drift gives409; I/O/DB failure503 allows explicit same-request retry.
- `POST /api/v1/wiki/pages/{proposal_id}/apply` body `{expected_proposal_sha256, expected_target_sha256}` -> updated target PageDetail. Require proposal's generated hash, target's originally proposed hash, current evidence, explicit user action. Server changes proposal Markdown ID to target ID/status reviewed; preserve target non-system metadata, replace body/generator metadata with reviewed proposal content. Destination as above. Keep proposal draft and old target recovery.
- Stable operation UUIDv5 of operation kind, page ID, manifest page ID and old/new hashes. Rendering must be deterministic across retry; store review time in DB, not changing Markdown per request.
- Scan duplicate identities without publishing a projection. Repair a duplicate target projection only after `ReviewFileStore.validate_retry_intent(operation_id, page_id, expected_sha256, markdown)` proves this exact durable intent and its old/new file state. An absent journal returns false; unrelated/proposal duplicates are conflicts and cannot repair tombstoned identities. File scan errors and limits return503; identity/hash conflicts return409.
- A network/503 failure in Web retains the exact mutation payload for **继续上次操作** within the mounted workspace. This never supplies new Markdown, and cannot bypass server hash/evidence guards. After browser reload, explicit API recovery uses the original retained manifest hashes; a persistent recovery UI is M5 work.
- Generation panel queues new topic or selected-page proposal, polls jobs with cleanup/abort and explicit retry, opens generated page on success. For selected generated page show each claim with matching quoted evidence, IDs/hashes/locations/index time; show stale/modified guard; explicit review/apply buttons use currently displayed hashes. Dirty editor disables mutations. Show bounded proposal diff and target drift; avoid untrusted HTML. No fake controls.
- Web props `page: PageDetail|null`, `dirty:boolean`, `onOpenPage(id:string):void`, `onChanged():void`. After successful actions refresh/open the returned page. Ordinary manual page has generation404 and remains usable.

Acceptance still requires real model generation and actual Web review/apply/conflict plus failure/retry/process recovery. Schema/unit tests alone do not accept M3.
