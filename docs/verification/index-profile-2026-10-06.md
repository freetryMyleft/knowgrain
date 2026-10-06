# Index profile verification — 2026-10-06

Scope: immutable, server-only configuration capture and comparison for the supported
raw-text Core indexing path. This is internal foundation verification, not complete
M5 rebuilding, provider-settings UI or normal-runtime configuration enforcement.

## Current reproducible checks

Executed from the repository with the existing verified local tokenizer cache:

```sh
TIKTOKEN_CACHE_DIR="$PWD/data/tokenizers" .venv/bin/python -m unittest \
  tests.test_index_profile tests.test_core_lifecycle tests.test_generation_runtime \
  tests.test_lightrag_core_maintenance tests.test_index_identity \
  tests.test_provider_roles tests.test_lightrag_entity_mapping \
  tests.test_core_inspection tests.test_application_runtime -q
uvx --offline ruff check src/knowgrain/index_profile.py tests/test_index_profile.py
uvx --offline ruff format --check src/knowgrain/index_profile.py tests/test_index_profile.py
git diff --check
```

- Final 114 tests passed in 10.931 seconds after review fixes (initial 112 passed
  in 5.385 seconds). This selection uses constructed pinned Core
  objects and controlled fixtures; it is not a live PostgreSQL/model acceptance run.
- Ruff passed; both files were already formatted; whitespace checks passed.
- Twelve profile cases cover actual defaults, immutable canonical roundtrip, no capture
  side effects, model revision binding, actual callback factory/closure and wrapped
  role arguments, three vector-store bindings, dirty prompt/config caches,
  fingerprint classification, malformed snapshots and safe public representations.
- Independent Python/general review found two missed effective inputs: role cache
  identities and custom wrappers hidden behind `@wraps`. Root fixed both after Luna
  dispatch was rejected by the host thread limit. Final finite Python/general
  reviews approved; Python independently executed the two added regressions.
  The fixes reject altered cache namespaces and inspect the pinned queue's real
  worker targets/parameters, including first capture and restored-JSON comparison.
- Final architect review approved the actual fixes and the internal-only delivery
  boundary. Review approval does not replace the executed checks above.
- Test setup verifies tokenizer size and SHA-256 before constructing Core. Missing
  or corrupt cache fails with an installation instruction instead of downloading.

## Historical isolated Core check

On 2026-10-04 Root observed a separate acceptance script successfully use the local
Ollama native embedding / compatible LLM callbacks with an isolated restored RAG
database and a distinct workspace/vector identity. Capture before initialization,
canonical roundtrip and comparisons before/after retrieval succeeded. Retrieval
returned seven entities, six relations and one chunk. Changing the summary-length
setting was rejected; restoring it matched again. Strict Core closure succeeded
and all eighteen retained Vault file hashes matched. The query could update Core's
keyword cache; no indexing or Vault write was performed.

On 2026-10-06 the prior temporary script, proof JSON, restored Vault and database
runtime directories were confirmed absent. That historical observation is retained
as context; it cannot be independently reread here and was not rerun today. Only
Ollama's 11434 listener was present; the earlier 8787 and 55433 listeners were absent.
This node does not claim a currently running Web service or a paid-provider test.

## Remaining acceptance

Persistent generation/selector records, coordinator reconstruction from the sealed
profile, guards for ordinary writes and reads, strict target database audits,
restart recovery, full rebuild and Web activation remain required. Existing legacy
indexes do not become verified merely because this module can capture a new Core.
