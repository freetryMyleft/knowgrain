# M1 Vault setup contract

## Decision and scope

Persist the selected Vault in the application database as a singleton binding (`vault_binding`, id=1, `binding_id` UUID, canonical `root_path`, created/updated timestamps). A bound database chooses this path on subsequent starts; `VAULT_ROOT` is the initial suggestion only. `VAULT_PARENT_DIR` (default `./data/vaults`) limits folder names the Web can select. A first configured Vault may live elsewhere via server configuration; the browser cannot browse arbitrary server paths.

Once any source record exists, changing the selected root is rejected with 409. Same-root selection is idempotent. Moving/restoring populated Vaults needs the later explicit M5 recovery workflow. No data is moved or deleted by setup. Avoid adding dependencies.

Initialization requires migration `0002_m1_vault_binding`. Whether adopting a legacy database or reopening an existing binding, validate every registered `source_revision` original under the selected root with safe paths and streaming SHA-256 before reporting ready. Repeat this validation for an idempotent same-root selection. A missing or changed original prevents adoption/readiness, uploads and index claims. Use the persisted root when bound, validate directory safety, and probe write access with a temporary file (remove only that probe). Never overwrite user files or `.obsidian`. This identity check does not repair or reset data; automatic reconciliation remains M5 work.

Binding changes must serialize with imports and reinitialization in the single backend process. Stop the index runner before changing its Vault object, rebuild SourceService/runner references, and preserve the existing LightRAG instance/event loop. Database binding writes use a transaction/advisory lock so binding creation is unique. Failure leaves uploads disabled until explicit retry or repair; no silent schema/data reset.

## Public API

All paths below are relative to `/api/v1` and use existing same-origin protection. No model secrets, arbitrary read/write APIs or directory-content enumeration.

- `GET /system/vault`: 200 status `{binding_id: string|null, root: string, configured_root: string, allowed_parent: string, ready: boolean, selection_enabled: boolean, directories: string[], detail: string|null}`. If application DB unavailable, respond 503 with a readable detail.
- `POST /system/vault/preview`: body `{name: string}`. Name is exactly one nonempty safe folder component, max 120 characters, no separators, traversal, control characters or Windows drive syntax; candidate must remain beneath canonical allowed_parent and contain no symlinks. Return `{name, root, exists: boolean, directories: string[], create_directories: string[], selection_allowed: boolean, binding_id: string|null, expected_root: string}`. `directories` lists every intended directory; `create_directories` is its missing subset. Preview is read-only: no files/directories created. Return 422 for unsafe paths, 409 if populated database forbids changing root.
- `POST /system/vault/select`: body `{name: string, expected_binding_id: string|null, expected_root: string}`. Compare current binding ID/root before mutation; stale ->409. Revalidate target and source count. Create only intended directories, verify writable, persist binding, rebuild application Vault services, start runner only when usable. Return status shape from GET. Disk permission/path errors ->422 with safe message; database failures ->503.

Directories: `Sources/Files`, `Sources/Evidence`, `Wiki/Drafts`, `Wiki/Pages`. Reject symlink components before canonicalization so selection cannot use a link to another location. Existing unrelated files remain untouched.

## UI contract

Add a real “Vault 设置” control reachable on desktop/mobile. A focused settings dialog/panel shows current root and permitted parent, a labelled folder-name field, “预览目录” and after success “使用此 Vault”. Preview lists exact target and folders that will be created, with existing/new distinction. Changing the input invalidates the preview. Selection submits the preview binding/root for conflict detection. Render pending/error/locked states and a retry path. Existing sources -> explain binding locked, keep current path visible. After selection refresh health/source state; never show success for failed 409/503. No filesystem path browser or mock install controls.

## Ownership

- Backend Luna owns `src/knowgrain/vault_setup.py`, `config.py`, `models.py`, `database.py`, `api.py`, migration `0002`, and focused Python tests (including required compatibility updates). Keep Core, repository and parsers unchanged unless Sol authorizes an interface fix.
- Frontend Luna owns `apps/web/src/App.tsx`, new `VaultSettings.tsx`, `styles.css` and focused frontend types if needed. No backend, manifests or lockfile edits.
- Sol owns main docs, `.env.example`, migration execution, runtime acceptance and integration/review.

## Acceptance

Actual PostgreSQL verifies persistent selection/restart, legacy original adoption, mismatched hashes fail closed, populated root change rejection, stale selection rejection, and safe-path errors. Unit tests cover read-only preview, symlink/traversal rejection and preservation of existing files. Test data and binding cleanup stay confined to explicitly disposable test databases. TypeScript/build and actual browser preview/selection verify the UI. Database migrations are explicit; remote CI and release checks are separate gates.
