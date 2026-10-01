# M1 Web implementation contract

This is the original source-import UI contract for M1. Real M0/M1 local model, import and Vault-selection acceptance has since passed; see `development-status.md` and `verification/`. This contract does not claim Wiki or question features are implemented.

## Ownership and direction

Luna owns `apps/web/` only. Sol owns backend integration, Makefile and main documentation. Preserve all other files.

Follow the macOS direction in `design/ui-concepts.html`: pale window chrome, quiet sidebar, toolbar and source inspector. Tokens: desktop `#e9edf2`, window `#f8f9fb`, content `#ffffff`, text `#1f2733`, secondary `#6f7885`, accent `#5265c9`. Use local system fonts (`-apple-system`, SF/PingFang fallbacks); UUID/hash fields use the system monospace font. The distinguishing element is the revision inspector showing latest uploaded and current indexed revisions with their real states.

Layout: sidebar for Sources and Service Status; source list in the middle; selected source details at right. Collapse naturally on smaller screens. Use real empty/error/loading states. Do not add fake navigation, sample rows, mock graphs, nonfunctional window buttons or placeholder settings forms.

## API

All calls use relative `/api/v1` paths. Vite proxies `/api` to `http://127.0.0.1:8787`, keeping the original Host header (`changeOrigin: false`) so browser Origin matches the backend request origin. Production assets will be served from the same FastAPI origin. No browser environment variable for secrets or DB paths.

- `GET /health/ready`: JSON fields `status`, `lightrag`, `postgres`, `ollama`, `app_database`, `vault`, `detail`. A 503 still returns useful status JSON. Model unavailability need not disable upload if `app_database` and `vault` are ready.
- `POST /system/retry-initialize`: same JSON; may return 503. Reconnect button should show the result and restart-required guidance.
- `GET /sources?limit=100&offset=0`: a list (not envelope) of source snapshots; fetch pages as needed. Each row: `source_id`, `filename`, `state`, `latest_revision_id`, `current_revision_id`, `revision_status`, `sha256`, `vault_path`, `error`, `latest_revision`, `current_revision`, `created_at`. A revision has `revision_id`, `filename`, `sha256`, `vault_path`, `media_type`, `index_state`, `parsed_text_sha256`, `error`, `created_at`, `indexed_at`.
- `GET /sources/{source_id}`: same snapshot.
- `POST /sources` and `POST /sources/{source_id}/revisions`: FormData field `file`. Return 202 `{source_id, revision_id, job_id, duplicate, vault_path}`. Supported MD/Markdown/TXT/PDF/DOCX; default max20 MiB but server setting is authoritative. No arbitrary path input.
- `POST /sources/{source_id}/reindex`: 202 `{job_id}`; already queued/running =>409.
- `GET /jobs/{job_id}`: `{job_id, revision_id, kind, state, attempts, error, created_at, updated_at, lease_until}`. Poll active job; source snapshots show states even if job_id not known after page reload.
- Errors normally `{detail:string}`; validation may return an array, handle safely with readable text. Do not display raw unescaped HTML or crash on unavailable backend.

## Implementation and acceptance

Use React, TypeScript and Vite, npm with committed `package-lock.json`. Only add packages needed for this slice; no editor/graph packages until those functions exist. Do not load remote fonts or runtime CDNs. Preserve browser escaping of filenames/errors. Use abort/cancellation or stale-result protection on async selections and polling.

User flows: empty Vault -> select/upload file -> server response -> selected source/revision -> live indexing state; duplicate -> explain reuse; update selected source -> new revision -> previous current visible while indexing; failed latest revision -> retry; unavailable service -> show which dependency failed and reconnect when possible. No claim that `queued` means indexed.

Keyboard access, labelled controls, visible focus, mobile layout and reduced-motion support required. Run actual TypeScript and production build checks. Report dependency choices and limitations; Sol verifies the UI in a browser against a real API before acceptance.
