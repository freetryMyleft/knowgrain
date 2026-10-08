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
