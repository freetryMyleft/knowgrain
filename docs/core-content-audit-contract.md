# Expected content and persisted Core audit

This extends the [complete rebuild contract](m5-workspace-rebuild-contract.md).
It preserves the distinction between expected data, a PostgreSQL observation and
a verified generation member. Internal modules alone do not provide a public
rebuild operation or authorize activation.

## E2: derive expected content from original bytes

The manifest builder receives a sealed `IndexProfile`, revision UUID, original
filename, Vault-relative display path, original bytes, their expected SHA-256,
and a validated local tokenizer/resource. It reads no Vault path itself. The
future executor must safely read the exact revision under the configured Vault
and must retain its operation/item grant across the subsequent write and audit.

Before parsing, the builder compares the sealed implementation versions and
source hashes with the actual installed implementation. It also checks the real
tokenizer class, model, encoding resource, ranks, special-token mapping and
pattern. A syntactically valid deserialized profile is insufficient. No parameter
is re-resolved from environment variables or a mutable live Core.

The supported production insertion contract is Knowgrain local parsing followed
by `ainsert(parsed.text, ids=[revision_uuid], file_paths=[vault_path])`:

1. Check the original byte SHA-256 and call `parse_document`.
2. Retain ordered parsed segments and the parsed-text SHA-256.
3. Apply Core `sanitize_text_for_encoding`; retain this RAW text and its separate
   SHA-256. Core's content-dedup MD5 is a third, distinct value.
4. Resolve slim fixed-token options from the sealed chunker configuration with
   empty `process_options`. Default `None`/`False` runtime split arguments retain
   the configuration defaults. Core does not reparse a PDF/DOCX here.
5. Run the pinned legacy six-argument fixed-token chunker with source spans.
6. Apply the pinned Embedding-token-limit fallback splitter and its overlap.
7. Build final stored chunk records, including positional IDs, order, tokens,
   exact content, revision parent and canonical Core display filename. Internal
   source spans are removed as in the actual write path.

The RAW `{{LRdoc}}` literal is ordinary text, not a structured-parser directive.
The manifest is immutable and its digest includes ordered segment locations and
final chunk bodies. Equal positional IDs do not prove equal bodies. Body/path
values do not belong in diagnostic representations or errors.

This is an expected-content manifest, not a persisted-data audit or receipt.

## E3: independently read and audit persisted data

The strict adapter must flush through the drained actual Core, then read committed
PostgreSQL data directly, with fixed source/schema and actual identity checks.
It must not call getters that swallow SQL errors, return buffered vectors or
generate an embedding to substitute for a missing vector.

For each member, compare full document content and hash, processed status,
document metadata, chunk manifest/order/count, every final chunk body/token/ID
and ownership with E2. Validate actual stored vector dimensions, finite values,
indexed text and metadata; do not regenerate embeddings as evidence of storage.

For the whole workspace, require exact expected document/status/chunk/vector
sets. Validate full entity/relation document anchors and chunk-tracking sets,
graph node/edge/endpoint sets and corresponding vector records. The capped
graph/vector `source_id` projection must match the pinned projection rules; it
may differ from complete `entity_chunks`/`relation_chunks` membership. Reject
unexpected namespaces, orphan records, extra records and malformed provenance.
An empty graph can be legitimate and requires a successful global read, not an
exception interpreted as absence.

Only the coordinator may record an audited member result after all applicable
checks. Workspace audit plus source snapshot/grant validation is required before
activation. D2 admission fences and external writer/DDL exclusion remain required
through inspection, initialization, writes, audit and selector transition.

### Internal E3 entry points (2026-10-09)

`knowgrain.core_audit` provides two independent observations:

- `audit_persisted_content(manifest, pg_config, identity, *, tokenizer,
  tokenizer_cache_dir)` checks one E2 revision and workspace structural integrity.
  A passing member report does **not** certify the complete workspace membership.
- `audit_persisted_workspace_content(manifests, pg_config, identity, *, tokenizer,
  tokenizer_cache_dir)` checks exact document/status/text-chunk/chunk-vector sets
  against the supplied complete, nonempty source manifest sequence. Its separate
  `WorkspaceAuditReport` contains manifest hashes, issues and a summary.

Both require one common sealed profile, unique revision identities, the validated
E2 tokenizer and an explicitly supplied local tokenizer resource. Missing inputs,
changed implementations/resources, mismatched resolved workspace, unsupported
schema, query failure and cleanup failure reject with a static `AuditError`.
There is no environment re-resolution, tokenizer download or model invocation.
Empty expected workspaces require a future explicit empty-workspace contract.

The adapter uses a separate bounded asyncpg connection, `repeatable_read` and
`readonly=True`. Catalog checks identify the actual database, public schema,
pgvector extension types, permanent tables/columns, primary keys, row security
and graph endpoint constraints/enforcement. Data reads explicitly qualify public
and scope every query to the target workspace. Target data in another vector
model family and graph namespaces other than `chunk_entity_relation` reject.
Other workspaces may contain data and are not compared with the target manifests.
The connection closes on errors/timeouts/cancellation, and failed close forces
termination. PostgreSQL diagnostics and stored body/path/entity values are not
included in findings; issues expose static messages and optional aggregate counts
only; reports contain manifest hashes, issues and a summary.

| Surface | Internal observation |
| --- | --- |
| Documents/status | Exact RAW body/hash/path/options, processed status, length, ordered chunk list/count and parse metadata; no custom engine or sidecar. |
| Chunks | Exact E2 IDs/body/tokens/order/owner/path, RAW metadata and processed-document ownership. |
| Vectors | Actual stored vector text, configured vector/halfvec type and dimension, finite values, exact chunk-vector identities/body/metadata. |
| Graph | Recovery-anchor document sets and union, nodes/edges/endpoints, corresponding vector/tracking sets and complete tracking ownership. |
| Provenance | `<SEP>` relation keys; ordered, unique complete tracking IDs; sealed KEEP/FIFO capped graph projection and corresponding vector `chunk_ids`. |
| File provenance | Every real graph path belongs to complete tracking chunk sources. Display paths are nonempty, unique source-member subsets bounded by sealed `max_file_paths`; historical subsets may omit a marker. An optional single marker must use a fixed KEEP/FIFO format and sealed placeholder. Marker-only paths reject unless the sealed limit is zero. Corresponding VDB path strings must equal graph paths. |
| Indexed graph text | Entity name/description and normalized relation keywords/endpoints/description, truncated using the sealed tokenizer and Embedding token limit; no embedding regeneration. |
| Runtime caches | `llm_cache_list` is an ordered unique string list whose references exist and belong to that chunk; cache IDs have a namespace separator and nonempty chunk references do not dangle. Cache bodies/LLM output semantics are outside E2 expectations. |

Extraction changes `llm_cache_list` after E2, so it must not be compared with E2's
initial empty list. Cache observations do not prove that model output was correct.
Likewise graph structural consistency does not prove semantic extraction
completeness: E2 cannot predict which valid entities or relations an LLM will find.

```python
report = await audit_persisted_workspace_content(
    expected_manifests, resolved_pg_config, target_identity,
    tokenizer=validated_tokenizer, tokenizer_cache_dir=local_resource_directory,
)
if not report.passed:
    # Keep target inactive; retain failed task state for explicit recovery.
    handle_audit_failure(report)
```

The caller must flush the drained actual Core before calling and retain its
source snapshot, grants and writer/DDL exclusion. These APIs neither verify those
caller conditions nor record member success, change the ledger or activate a
selector. Normal Runtime, workers and public APIs remain unconnected.

Path checks are source-membership observations, not replay of historical merge
order. Pinned normal merges use `(KEEP Old)` / `(FIFO)` markers; surviving-chunk
rebuilds use `(KEEP n/total)` / `(FIFO n/total)`. Both known forms are accepted
under the sealed method and placeholder. Numeric markers require the sealed
limit as numerator and an integer denominator strictly larger than the numerator;
the denominator records historical source count and need not match the current
tracking candidate count. No arbitrary
marker, foreign path or oversized path list is accepted. Normal incremental
entity updates can add tracking/source IDs while retaining the previous display
path, so even below the cap a historical subset is legitimate. These observations
do not prove the display path list is complete and cannot detect synchronized
removal of one valid display path. Accurate evidence traversal uses complete
tracking chunk IDs, not the display path list. Single-source nonzero-limit paths
still match exactly. The profile already seals all three required path knobs.

The current `_audit_connection` fetches each workspace surface fully into memory.
This is an internal, moderate-scale implementation boundary. Before production
activation integration, implement streaming/pagination or pass explicit scale and
peak-memory acceptance; a connection timeout does not bound validation memory.

Initial verification covers fixed-schema synthetic committed PostgreSQL fixtures,
corruptions and read-only preservation, including eleven PostgreSQL cases with
selectively synchronized A-node/A-vector path corruption after a passing two-source
historical-path observation, plus offline validation/connection failure
cases. A real Core `ainsert` fixture and complete coordinator activation/recovery
acceptance remain required before describing E3 as production-integrated.

## Content deduplication requires a production decision

Pinned Core deduplicates normalized RAW content across filenames even when the
caller supplies distinct revision IDs. Different original files can therefore
have distinct source SHA-256 values but the same Core content-dedup key (including
after sanitization). A duplicate attempt can produce a failed `dup-*` status row
without a processed record for the requested revision.

E2 exposes this key so the executor can detect the conflict. The rebuild must not
skip one active/latest source or claim its own revision is verified through an
unrelated processed document. Production integration must choose and verify a
policy that preserves every accurate revision and provenance. Any change to the
insertion contract or shared-content ownership requires an explicit profile,
audit and cleanup design; it cannot be hidden in this manifest builder.

## Acceptance boundaries

E2 tests cover original formats and locations, text normalization, overlap,
character splitting, hard Embedding splits, exact IDs and tokenizer/implementation
rejections without models or databases. E3 needs actual PostgreSQL/Core fixtures
and corruption cases across every persisted surface. Final rebuild acceptance
still requires multiple active sources, interruption/resumption, atomic
activation, exact evidence and unchanged original/reviewed file hashes.
