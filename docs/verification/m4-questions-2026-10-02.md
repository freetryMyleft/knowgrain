# M4 question node verification — 2026-10-02

This accepts the local question/evidence implementation node, not complete M4 or M0–M6. Wiki/Core entity mappings, full provider configuration, recovery/evaluation and release remain required.

## Environment and checks

- Pinned embedded LightRAG 1.5.7, one backend event loop, Ollama `qwen3.6:35b`, unchanged `qwen3-embedding:0.6b` / 1024 dimensions.
- PostgreSQL 16.14 + pgvector, disposable tests `knowgrain_test` on 55432 and isolated acceptance application `knowgrain_test_setup_ui` on 55433; existing Core database and Research Vault reused.
- Explicit Alembic upgrade `0007_m3_review -> 0008_m4_queries` on both application databases. Acceptance `alembic check`: no new upgrade operations. Runtime did not reset indexes or auto-migrate.
- `KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=55432 .venv/bin/python -m unittest discover -s tests -v`: **198 passed**, 14.442 seconds. Log: `/tmp/knowgrain-m4-question-tests-final.log`. Initial run had two fixture errors (missing import and a chunk identity mismatch); both were corrected without weakening validation.
- `npm run build`: typecheck and Vite production build passed. Wiki chunk remains above Vite's size advisory (700.14 kB, 233.21 kB gzip), while questions are a separate lazy chunk.
- `node --experimental-strip-types --test checks/question-contract.test.mjs checks/generation-contract.test.mjs checks/wiki-contract.test.mjs`: **13 passed**.
- New meaningful cases include strict answer/citation JSON, no-evidence behavior, stale evidence during generation, invalid model output failure, cancellation joining publication threads, safe HTTP attachment headers, immutable PG evidence/results, expired lease reclaim, original/Markdown changes, leaf/parent symlinks, append during reads and captured-byte behavior after a subsequent path replacement.

## Actual model and Web workflow

1. In the real Web question form, submitted a question about the two facts explicitly stated by the synthetic acceptance original. Job `1e78bcfe-a19b-4191-99d0-1d91c5a9cffa` completed in one attempt, from 02:42:48 to 02:43:46 UTC. Actual model output retained two claims: the original is in Research Vault, and Web should show the latest revision consistent with the current index after indexing.
2. Both claims cite `9f896323-dc17-56ce-8103-79b585c86f7d`, source `2b10760a-78b5-48e8-8a58-036854c1a920`, revision `56ffa83e-99f2-48dd-87cf-e888070aaf87`. Web showed model/provider/time, revision and indexed time, and the shared dialog displayed the exact 64-character quotation, full hashes, Vault path and character offsets 0–64.
3. A separate real question about backup retention days (absent from the supplied original) produced job `84270e60-8cc3-482e-b2f0-66d0b1c24735`, succeeded once, with status `insufficient`, the exact server-owned message, and no claims/evidence. It did not invent a retention period.
4. Reloaded the Web page, reopened questions and recovered both saved records from PostgreSQL. The most recent insufficient answer and the selected supported answer displayed correctly. Selecting history cannot render the prior task's answer under a new selected ID.
5. Clicked original download in the dialog. The server returned HTTP 200 without browser console errors. The in-app download observer did not emit a completion event, so a native browser filesystem save location is **not verified**. Browser attachment compatibility remains a focused follow-up.
6. Independently fetched the real original and evidence Markdown endpoints and verified captured HTTP bytes: original 135 bytes, SHA-256 `445e2deb31d2aa58b999ca35fec387846e46fb1a869f714ec18c348c6aef2f57`, matching the evidence revision. Canonical Markdown 1032 bytes, SHA-256 `026656d07cf98a6a32394cba0f18c8721d84560d1e21ac7180c636b9ec822799`. Both use attachment disposition, `nosniff` and `no-store`; arbitrary file paths are not accepted.
7. Already reviewed Wiki `2e7b533a-b379-426a-8912-efcd4a1bc75d` retained SHA-256 `e9bc26be36e0752123ab7534d115cd336042d9590d8a97d3a40af40202e6897d`. Questions did not modify its body or status.
8. Opened the reviewed Wiki's rendered **证据 1** link and confirmed it opened the same evidence dialog and exact revision. Closed and restarted only the positively identified owned API process after both question jobs completed. Core finalized all 12 storages and closed the pool. The restarted service was ready; both jobs recovered with their original answered/insufficient results and current flags, and the reviewed Wiki hash was still unchanged.

Synthetic acceptance snapshots and downloaded bytes are under `/tmp/knowgrain-m4-acceptance`; they are not committed as user content. Screenshots were inspected through the browser tool and not saved as repository artifacts. Obsidian desktop itself was not operated.

## Review and boundaries

Root defined the API and ownership contract, Luna/xhigh implemented persistence/files and query service/API, and Root implemented/wired the Web UI. A third worker and specialist review dispatch hit the host agent thread limit. Existing Luna agents performed independent finite read-only reviews of frontend and persistence; Root inspected the actual integration diff. Findings fixed: stale selection display, weak message guards, mismatched repository claim limits/keys, API pagination limit, repetitive per-quote file parsing and cancellation propagation. No additional independent specialist approval is claimed.

This sample checks real execution and directly supported claims; it does not establish the M6 30-question accuracy target. Deleted/stale predicates are enforced through shared provenance and PostgreSQL checks, but the public soft-delete/rebuild/recovery workflows remain M5. Docker PostgreSQL 18, remote CI, third-party providers and native browser download portability remain unverified.
