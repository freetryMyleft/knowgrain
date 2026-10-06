# Index profile contract

Status: internal configuration foundation. `IndexProfile` does not yet guard the
normal application runtime, persist generations, rebuild indexes or expose a Web
model-settings API. Those integrations belong to the M5 rebuild workflow.

## Supported indexing path

The profile captures the actual constructed `lightrag-hku==1.5.7` Core used with
Knowgrain's independent provider callbacks. The first contract supports local
Markdown/TXT/PDF/DOCX extraction followed by `ainsert(rawtext)` with empty
`process_options`, the built-in six-argument legacy token chunker and the fixed
`gpt-4o` / `o200k_base` tokenizer. It rejects custom tokenizers/chunkers, role
overrides, multimodal processing and unknown add-on settings.

Capture reads already-resolved configuration. It does not construct or initialize
a Core, invoke a model, write a file, download a tokenizer or call upstream helpers
that mutate caches or deep-copy live runtime objects. The tokenizer resource must
already be installed and match the pinned SHA-256 (`make tokenizer`).

## API

Implementation: `src/knowgrain/index_profile.py`.

- `IndexProfile.capture(rag, callbacks, tokenizer_cache_dir, model_revisions=None)`
  seals the constructed Core, actual provider bindings and effective configuration.
- `to_canonical_json()` / `from_canonical_json()` provide immutable, validated,
  deterministic server-side serialization. Restore rejects unknown fields,
  duplicate keys, invalid types, nonfinite numbers and incoherent contracts.
- `compare(...)` returns changed field paths and fingerprint categories.
  `assert_matches(...)` rejects any difference with a safe field-path diagnostic.
- `public_summary()` exposes fingerprints and provider/model names only. Full
  canonical JSON includes resolved prompt text and provider addresses; keep it in
  server-owned storage and never return it to the browser or write it into a Vault.
  Credentials are excluded from snapshots, fingerprints and object representations.

Capture verifies the callback factory's function code and bound configuration,
the actual wrapped callbacks and sampling arguments for all four Core LLM roles,
and the shared embedding binding of all three vector stores. Priority wrappers must
match the pinned implementation's function code, actual worker closure targets and
effective queue parameters; an arbitrary `@wraps` function is rejected. Role cache
identity must remain the default namespace with the configured model; non-default
provider/address/model metadata and unknown metadata keys are rejected.
In-process comparisons
also check object identity; a restored snapshot compares configuration across
processes without treating previous process objects as persistent identities.

## Fingerprints and model revisions

| Fingerprint | Meaning |
| --- | --- |
| `content_embedding_fingerprint` | Parser/package/source versions, segment and chunk identity contracts, effective tokenizer/chunking limits, embedding model/revision/dimension/prefixes. Embedding request timeout is excluded. |
| `graph_write_fingerprint` | Implementation and graph prompt hashes, graph extraction/merge limits, resolved language/prompts, LLM model/revision and extract-role sampling. Query-only sampling is excluded. |
| `llm_fingerprint` | Complete LLM role configuration and supplied model revision. |
| `snapshot_fingerprint` | Entire canonical snapshot, including operational settings. |

Source hashes are deliberately conservative: implementation edits may require
compatibility review even when one runtime field is unchanged. Classification does
not grant permission to resume a changed snapshot; `assert_matches` currently
requires a full match. A future coordinator must define explicit policy before
allowing a query-only or operational change.

`ModelRevision` binds an observed revision to its exact provider/address/model.
Provenance is `ollama-tag`, `operator` or `unavailable`. Capture makes no network
request to discover a revision. Missing information remains explicitly unknown;
a mutable remote model alias is not proven immutable by hashing its name.

## Failure and recovery integration

For a future verified generation, the coordinator must construct provider callbacks
and Core from the sealed configuration, resolve secrets separately, capture before
storage initialization and check again before writes. On restart it must compare
the actual environment, implementation, tokenizer and model observations with the
stored snapshot. A mismatch keeps the rebuild frozen and reports field paths;
resuming with different settings requires an explicit compatible recovery or a new
rebuild. This module alone does not enforce those lifecycle rules.

No database migration, dependency or public API was introduced by this node.
Current normal runtime still uses the existing Ollama configuration path.
