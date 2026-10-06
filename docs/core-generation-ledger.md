# Core generation ledger — D1

Status: internal M5 foundation. The normal Runtime, source/model/file workers and
HTTP routes do not consume this ledger yet. Migration `0013_core_generations`
creates empty ledger tables and compatible nullable execution fields; it does not
create a selector, infer a model profile or switch an index.

## Persisted records

| Record | Purpose |
| --- | --- |
| `CoreGeneration` | Unique workspace, vector token and working directory; server-only canonical profile and four fingerprints. `sealed` means configuration recorded, not data verified. |
| `CoreSelector` | Singleton active/pending identity, request CAS version, separate execution epoch, freeze and persistent restart latch. |
| `RebuildOperation` | Original request digest, target identity, Vault binding/root snapshot, administrative version/retry replay, source snapshot and parent execution lease. |
| `RebuildItem` | Exact source/latest revision/lifecycle/hash/path and original parse metadata; per-item ownership, parent claim binding, attempt results and cleanup manifest. |
| `CoreGenerationRevision` | Durable possibly-touched/write-intent record per generation/revision, parsed attempt data and retained cleanup IDs. |

Compound foreign keys enforce source/revision and operation/target ownership.
Named checks validate counters, profile/lease groups and audited-state metadata;
leading lookup indexes cover new foreign keys. Restrictive ledger foreign keys
retain identity and partial-write history.

The existing index/maintenance/file/generation/query task tables gain nullable
generation/epoch/token fields, a zero-default claim fence and retirement metadata.
`SourceRevision` gains nullable `indexed_generation_id`. Existing states, manifests,
IDs and content/index timestamps are retained. These fields alone provide no
ordinary-worker fencing until D2 propagates and validates grants.

## Internal repository flow

`CoreGenerationRepository.bootstrap_legacy(observed_identity)` serializes singleton
initialization and rejects a differing later identity. It records old revisions as
conservative `write_intent` members with no physical/verification timestamps or
profile. Ordinary task/revision backfill is deferred to coordinated integration.
Future startup must call it before admitting work; current startup does not call it.

`RebuildRepository` provides:

1. `request_rebuild`: resolve exact request replay first, then check selector CAS
   and Vault binding, allocate/persist one target and freeze. A changed payload using
   the same operation UUID fails. No model, Core or Vault operation occurs.
2. `claim_operation` / `renew_operation` / `fail_operation` / `retry`: new random
   token and monotonic claim fence for each claim. Administrative retry preserves
   the target and snapshot; recorded retry version supports exact replay. Heartbeat
   does not advance the administrative version. Restart-required failure remains
   frozen; there is no latch-clearing shortcut in this node.
3. `seal_snapshot`: require coordinator quiescence plus database checks, capture
   every active/latest source (including queued/failed latest revisions), persist
   items and digest, clear captured current pointers and advance execution epoch in
   one transaction. Invalid active/latest mapping fails. Replaying a sealed snapshot
   does not advance epoch or recapture sources. Empty range is explicit.
4. `claim_item` / `renew_item` / `begin_write` / `record_manifest` / `fail_item`:
   bind each item grant to the live parent token/fence, target, snapshot and epoch.
   Commit write intent before calling Core. Retain manifests after failure; another
   claim cannot overwrite a previous write attempt without the future strict
   absence audit. This node does not perform that audit or a Core write.

`status` and bounded UUID-keyset `list_items` expose safe progress fields. Canonical
profiles, paths, prompt text, parsed content and ownership tokens stay server-side.

## Fences, drain and recovery boundaries

`lock_index_fence` uses selector `FOR SHARE`; transitions use `FOR UPDATE`. Callers
lock selector before source/task rows. All execution mutations validate full grants
using `clock_timestamp()` **after their final source/item/member lock**. Owner UUID
alone is insufficient, including a same-owner reclaim after expiry. Item mutation
also validates the parent's current claim, so parent retry/reclaim invalidates old
child grants even when their own lease remains live.

Preparing-stage drain uses generation plus execution epoch, not request CAS version.
After snapshot sealing old ordinary grants fail. Pending operation, freeze, Vault
binding and sealed target profile are checked in the corresponding transactions.

`QuiescenceGuard` is an abstract contract for a future coordinator that holds
admission closed until snapshot commit and checks actual Core/model/file/review
execution has drained. There is no production implementation yet. Database running
rows block snapshot sealing even after lease expiry; queued file and prepared
review journals remain retained. Lease expiry never proves a thread or external
Core call has stopped. Before E reclaims execution it must drain actual old work.

No repository method marks a member verified/cleaned, completes an item as verified,
succeeds a rebuild or activates a generation. Those transitions require E's strict
database audit/absence receipts. Models reserve their eventual states and metadata;
neither caller-supplied JSON nor a successful ordinary Job is a receipt.

## Migration and remaining integration

Back up databases and Vault, stop application writers, then run `make migrate`.
Startup requires `0013_core_generations` and never migrates automatically. Downgrade
locks affected tables before checking population and refuses any ledger record or
new execution metadata. Empty-ledger rollback is an explicit maintenance action.

D2 must connect all ordinary source/maintenance/file/evidence/publication fences,
complete generation-bound service installation and selector-aware startup together.
E must provide strict persisted-data audits, cleanup receipts, actual quiescence,
sealed-profile reconstruction and rebuild execution/activation. Only then can Web
expose the rebuild workflow. See [full contract](m5-workspace-rebuild-contract.md)
and [verification](verification/m5-generation-ledger-2026-10-06.md).
