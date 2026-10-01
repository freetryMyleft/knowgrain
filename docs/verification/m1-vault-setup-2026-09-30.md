# M1 Vault setup acceptance — 2026-09-30

## Delivered flow

The Web header opens “Vault 设置”. Users enter a single folder name beneath the server's permitted parent, preview the existing/missing application directories, then select the Vault. Preview is read-only. Selection persists a singleton database binding, updates the import/runner Vault references after commit, and preserves the same embedded LightRAG instance. Any existing source record locks root changes.

The new explicit Alembic migration is `0002_m1_vault_binding`. `VAULT_ROOT` is the initial suggestion; a bound database restores its own root. Startup and idempotent selection verify all registered originals using safe paths, regular-file checks and SHA-256. Missing/changed originals stop uploads and index claims without deleting or resetting files/metadata.

## Actual environment

- Isolated PostgreSQL 16.14 from the previous [M0/M1 run](m0-m1-local-2026-09-30.md).
- Unit/transaction tests: `knowgrain_test`, loopback port 55432, migration 0002.
- Browser acceptance: separate database `knowgrain_test_setup_ui`, port 55433, migration 0002; LightRAG database `lightrag_acceptance`, workspace `knowgrain_setup_acceptance`.
- Runtime root: `/tmp/knowgrain-local-runtime/pg16.14.z8L19v` (macOS canonicalizes this to `/private/tmp`).
- Local models: `qwen3:8b`, `qwen3-embedding:0.6b`, 1024 dimensions. API binds `127.0.0.1:8787`.

Only synthetic fixtures were used. The separate source acceptance database from the prior run was not changed by this feature's tests.

## Browser and filesystem observations

1. Initial startup bound `setup-acceptance/initial-default` and reported all health dependencies ready.
2. Prepared an existing permitted folder `choices/Research` containing `keep.txt`, `.obsidian/app.json` and `Wiki/Pages`.
3. Browser preview correctly marked Research and `Wiki/Pages` existing, with the other three intended directories to create. Direct filesystem checks confirmed no `Sources` directory had been created by preview and both fixture files were unchanged.
4. “使用此 Vault” selected Research. All four intended directories existed afterward; existing files matched their original bytes and no probe files remained.
5. Reusing the old preview/binding returned HTTP 409 and did not create its target. A traversal preview (`../escape`) returned 422 while the active Vault remained ready.
6. Previewed a nonexistent `ScratchPreview`, then changed the input. The preview and selection button disappeared; no target directory was created. Escape closed the dialog and focus returned to “Vault 设置”.
7. Uploaded `setup-upload.txt` through Web. Its original was under Research with a matching SHA-256. Source `2b10760a-78b5-48e8-8a58-036854c1a920`, revision `620ba66c-ca1f-4128-aba8-700c68672ff5`, hash `bbdbf0eef1a66153c5bad479fe34ec467be81681dc5102871299cdcd8ab280dc`.
8. Local LightRAG processing finished; latest/current revision IDs matched and the revision became ready. The settings dialog showed the location locked, and a different-root preview returned 409 without creating folders.
9. Mobile 390x844 inspection showed current root, locked explanation and close controls; document width and scroll width both measured 390. The temporary viewport override was reset. Snapshot: `/tmp/knowgrain-vault-settings-mobile-2026-09-30.jpg`.
10. Stopped the API cleanly: all 12 storages finalized and PostgreSQL pool closed. Restarted with `VAULT_ROOT` changed to `unused-after-restart`; the service restored the same Research root/binding, original current revision and all-ready health. The unused suggestion directory was not created.

Selected binding ID: `6b6fe54f-93f9-46b5-aa03-cd3efab50dd9`. The preview API exposes no arbitrary directory listing or file-content access.

## Revision polling follow-up — 2026-10-01

The initial acceptance exposed a frontend race: the list and completed job reflected a ready revision, while the inspector retained an indexing snapshot. `App.tsx` now bases polling on stable pending-state booleans, keeps detail cancellation scoped to the selected source, and refreshes details when polling reaches a terminal state.

After loading the rebuilt frontend, Web “上传新修订” uploaded `setup-upload-next.txt` to the existing source. The inspector first showed the new pending revision alongside the old indexed revision. Without a manual refresh, both cards and the list then displayed “已索引”, and the job displayed “索引任务已完成 · 已尝试 1 次”. Latest/current revision IDs, hashes and Vault paths matched:

- Revision: `56ffa83e-99f2-48dd-87cf-e888070aaf87`.
- SHA-256: `445e2deb31d2aa58b999ca35fec387846e46fb1a869f714ec18c348c6aef2f57`.
- Job: `08ef670a-f779-494e-b179-a966e78acd71`.
- Screenshot: `/tmp/knowgrain-m1-revision-polling-2026-10-01.jpg`.

The same local API and embedded Core processed this revision. No process restart or database status edits were used to reach the completed UI state. The final setup acceptance now has one source with two revisions. This follow-up verifies successful completion synchronization; it does not prove every slow-network or source-switch race. Root inspected the request/selection guards. Both additional review-agent attempts failed at the account usage limit, so those reviews are not claimed as passed.

## Checks and reviews

- `KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=55432 uv run --locked python -m unittest discover -s tests -v`: all 54 passed (45 unit + 9 actual PostgreSQL tests).
- `POSTGRES_PORT=55432 KNOWGRAIN_POSTGRES_DB=knowgrain_test uv run --locked alembic check`: no new upgrade operations detected.
- `npm run typecheck` and `npm run build` in `apps/web`: passed after the final UI fixes.
- Python compilation passed; code/Python/TypeScript reviews approved after fixing original integrity gates, actual-directory write probes, unused configuration handling, FIFO blocking, stale dialog responses and malformed JSON handling.
- FIFO regressions use bounded subprocesses; static linters unavailable on this host were not claimed as passed.

## Limits

No populated Vault migration or hot switch is offered; moving/restoring contents remains explicit M5 work. Full dependency/model installation wizard, Wiki/editor/backlinks, evidence generation, questions, third-party adapters, backups and release evaluation remain incomplete. PostgreSQL 18 Compose and remote CI were not run. This is temporary local acceptance, not durable deployment to a clean machine.
