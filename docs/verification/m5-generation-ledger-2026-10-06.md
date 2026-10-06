# D1 generation ledger verification — 2026-10-06

Scope: internal persistent identity, operation/item ownership, transactional freeze,
source snapshot and partial-write intent/manifest foundation. No production Runtime,
Core switch, new HTTP endpoint, actual rebuild or Web acceptance is claimed.

## Environment and executed checks

Local PostgreSQL16.14 listens on127.0.0.1:55432, with explicitly selected disposable
`knowgrain_test`, migrated to `0013_core_generations`. The ledger is application
metadata; these checks do not need pgvector or initialize Core/model storage.
The existing fixed tokenizer cache is verified before constructing a controlled
Core object for a valid profile. Docker's daemon was unavailable.

```sh
KNOWGRAIN_TEST_DATABASE=knowgrain_test KNOWGRAIN_TEST_POSTGRES_PORT=55432 \
TIKTOKEN_CACHE_DIR="$PWD/data/tokenizers" .venv/bin/python -m unittest \
  tests.test_core_generation_schema_postgres tests.test_rebuild_repository_postgres \
  tests.test_sources_postgres tests.test_core_maintenance_postgres \
  tests.test_source_file_repository_postgres tests.test_generation_repository_postgres \
  tests.test_query_repository_postgres tests.test_review_repository_postgres \
  tests.test_provenance_postgres tests.test_reconciliation_postgres -q
```

- The executed combined selection passed67 tests in12.475s before four additional
  repository cases were added. Those four subsequently passed in two targeted runs
  (2/2 in1.073s;2/2 in1.028s). This is71 distinct passing checks, not a claimed
  single71-test run. The repository now contains13 cases; schema contains9.
- Source upload/cleanup/file replay/generation/query/review/provenance/reconciliation
  PostgreSQL regressions passed with the new additive ORM fields and schema head.
- Ruff and formatting passed on seven changed/new Python files other than the
  existing database module, whose only change is the schema-head string. Its
  preexisting lint findings were not presented as new failures or silently fixed.
- Root ran `alembic check`: no new upgrade operations. Whitespace checks passed.

## Meaningful ownership checks

- Concurrent bootstrap gives one observed legacy identity and conservative intents,
  without profile, physical/verified timestamps or ordinary-task retargeting.
- Concurrent rebuild requests accept one CAS; exact replay after selector version
  advancement retains its target; conflicting replay and frozen admission fail.
- Same-owner operation reclaim changes token/fence; the previous parent grant fails.
  Retry retains target identity and replays the same administrative request.
- Active/latest snapshot excludes deleted and historical revisions; invalid mappings
  fail. Expired running jobs block sealing. A controlled undrained witness fails.
  Sealed replay preserves digest/epoch; empty range is explicit.
- Parent reclaim invalidates an otherwise live child. Repository-level use of an
  existing item belonging to another operation fails.
- Write intent is required before manifest recording; failure retains cleanup IDs.
  New claim cannot overwrite prior possibly-touched data without future absence
  audit. Config fingerprint drift blocks parent claim and item write intents.
- Tests observe an actual PostgreSQL lock wait, then let the lease expire before
  releasing source/item/member locks. Final mutation fails and metadata is unchanged.
- Prepared review and queued file journals survive snapshot sealing. The review
  fixture's original and Wiki bytes/hashes remain unchanged; this is a controlled
  journal test, not an operated review UI or complete backup/restore drill.
- Restart latch remains frozen and rejects retry. No success/verification/cleanup
  or activation path is available in this repository.

## Migration preservation

Luna used a separate self-created `knowgrain_ledger_migration_test` database with
owned synthetic rows in eight preexisting tables: source_document/source_revision,
job/core_maintenance_job/source_file_operation, wiki_page/generation_job/query_job.
The initial seed attempt failed on JSON SQL parameter parsing and rolled back;
shared fixture then received an empty upgrade. A repaired independent fixture
performed populated0012→0013, then an explicit empty-ledger downgrade→final upgrade.
All original columns/rows matched the pre-upgrade JSON snapshot, all five new
ledgers were empty and execution fields remained null/zero.

Root read the comparison scripts and reran the final comparison successfully. The
scripts/snapshot are temporary `/tmp/knowgrain_*preservation*` artifacts, not release
utilities. Durable schema tests cover model parity, compound identity/lease/check
constraints, FK lookup indexes and populated/execution-metadata downgrade refusal.
This drill covers eight seeded tables, not every populated production table or
Vault/Wiki/evidence file preservation during a real rebuild.

## Reviews and limits

Requested coding route: worker `gpt-6-luna` / `xhigh`, host default priority channel;
no separate fast-mode parameter. Root implemented and integrated repository/fence
code. Independent Python/general/database reviews and architect review approved
the dormant foundation, with actual checks supplied by the executing agents.

`QuiescenceGuard` is controlled in tests. Actual Core/model/file/review draining,
process-restart continuation, all ordinary worker fences, strict persisted vector/
graph audits, activation, provider settings, complete rebuilding and M6 remain
unverified/unimplemented. Existing entity navigation does not complete the graph UI
or automatic Wiki relationship requirement. The full M0–M6 goal remains active.
