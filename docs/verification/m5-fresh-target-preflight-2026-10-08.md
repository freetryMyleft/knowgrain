# M5 fresh-target preflight verification — 2026-10-08

## Accepted scope

Internal E1 inspection of an existing shared PostgreSQL schema before first
initialization of a fresh rebuild workspace. The implementation is
`src/knowgrain/core_preflight.py`; its [contract](../core-fresh-target-preflight.md)
defines supported configuration and rejection rules. Normal Runtime, workers and
public APIs do not call it. It does not initialize LightRAG, create an audited
ledger receipt or activate a generation.

## Actual environment

The test database was `knowgrain_audit_test` at `127.0.0.1:55434`, isolated from
the retained application and earlier ledger databases. PostgreSQL 16.14 and
pgvector 0.8.7 were built for native arm64 under ignored
`data/development-runtime/`; installation did not change host global binaries or
extensions. Actual server version and extension queries confirmed these versions,
and a vector distance query returned 1 for `[1,2,3]` and `[1,2,4]`.

This establishes the local test environment. It does not verify the Docker
PostgreSQL 18 release image, a new-machine installation, remote TLS or production
Core initialization. The implementation guards `lightrag-hku==1.5.7` and the
exact installed PostgreSQL storage source hashes before deriving DDL contracts.

## Commands and results

Root independently ran:

```sh
KNOWGRAIN_AUDIT_TEST_DATABASE=knowgrain_audit_test \
KNOWGRAIN_AUDIT_TEST_PORT=55434 \
.venv/bin/python -m unittest tests.test_core_preflight tests.test_core_preflight_postgres -q
```

Result: **25 tests passed in 9.670 seconds**, with no skipped selected tests.
The retained local log is
`data/development-runtime/preflight-root-final-20261008.log` (ignored).
Asyncio debug slow-task notices during catalog-fixture DDL did not fail tests.

Additional checks passed:

```sh
uvx --offline ruff check src/knowgrain/core_preflight.py tests/test_core_preflight.py tests/test_core_preflight_postgres.py
uvx --offline ruff format --check src/knowgrain/core_preflight.py tests/test_core_preflight.py tests/test_core_preflight_postgres.py
.venv/bin/python -m compileall -q src/knowgrain/core_preflight.py tests/test_core_preflight.py tests/test_core_preflight_postgres.py
git diff --check
```

## Coverage and limits

- Configuration checks cover effective workspace mismatch, unsupported connection
  options, vector strategy, source pin, identifiers and hidden credentials.
- Real PostgreSQL fixtures cover all eight KV/status/cache tables, two graph
  tables and six legacy/other-model vector tables. Successful inspection preserves
  all sixteen tables' rows, object/physical identities and constraint definitions,
  including a reversed graph edge in another workspace.
- Actual rejection cases cover target workspace pollution across table families,
  existing target tables, empty legacy base tables, cross-schema ambiguity,
  incompatible columns/nullability, legacy migration hazards, row security,
  incorrect named graph keys and endpoint mappings, and disabled FK enforcement.
- A real exclusive lock exercises bounded timeout and cancellation. The captured
  cancelled inspection connection is actually closed; inspection succeeds after
  releasing the lock. Unit tests also cover cleanup failure with termination and
  preservation of the original cancellation.
- Missing extension/schema and privilege failures include simulated cases; they
  are not claimed as actual server privilege/extension fault drills.
- Fixture vector rows may have NULL vectors. This proves schema/namespace checks
  and data preservation, not healthy indexed content, finite embeddings, complete
  graph provenance or successful Core startup.

Independent Python, general and database reviews approved this internal scope.
Final read-only architecture review using GPT-6.1 Sol / medium also approved it,
checked the retained test log and confirmed the absent Runtime/API integration.
The report is a
snapshot observation: admission freeze, external-writer exclusion across actual
initialization, persistent member/workspace audits, generation activation and
restart recovery remain required by the complete rebuild contract. Automatic
Wiki relationships, full graph UI and M6 evaluation/release also remain open.
