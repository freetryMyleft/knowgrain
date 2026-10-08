# Fresh rebuild target preflight

Scope: internal E1 inspection before initializing a new rebuild target in an
existing LightRAG PostgreSQL database. It does not initialize Core, change the
generation ledger, verify indexed content or activate a workspace. Normal Runtime
and public APIs do not call it yet.

## Inputs and supported connection semantics

`ResolvedPostgresConfig.from_upstream()` consumes the actual
`ClientManager.get_config(vector_storage="PGVectorStorage")` result after the
future coordinator has fixed the target environment. It does not resolve a second
set of environment defaults. The selected workspace must agree with
`CoreIndexIdentity`; a populated upstream workspace overrides storage constructor
values and therefore cannot be discarded.

The current implementation supports a public-schema, UTF-8 database with default
schema resolution, empty server settings and SSL mode unset or `disable`.
Certificate settings, custom server settings and other SSL modes are explicitly
rejected. This is a local deployment boundary, not verification of remote database
or TLS configuration. Supported vector strategies are HNSW, HNSW_HALFVEC and
IVFFLAT; other strategies are rejected before connecting. Passwords remain in the
server-side configuration and are excluded from its representation.

`inspect_fresh_rebuild_target(config, identity, embedding_dim)` opens a separate
direct asyncpg connection and a bounded `repeatable_read`, read-only transaction.
It closes that connection on success, rejection, timeout and cancellation; failed
closure forces termination. Server diagnostics and row content do not appear in
public errors. It does not invoke upstream getters that can swallow SQL errors,
return buffered vectors or regenerate embeddings.

## Conditions checked

The fixed package version and both PostgreSQL storage source hashes must match
the supported implementation. DDL column specifications come from that guarded
source; unknown implementations do not silently reuse an older schema contract.

| Surface | Required condition |
| --- | --- |
| Schema resolution | Public is the only explicit resolvable schema; catalog resolution is unambiguous; matching LightRAG objects outside public are rejected. |
| Extension | Actual vector and halfvec type OIDs belong to the vector extension in public. A matching type name alone is insufficient. |
| Eight KV/status/cache tables | Ordinary permanent tables with the expected columns, types, nullability and primary-key order; no row security. |
| Two graph tables | Expected named primary keys and foreign keys, exact ordered endpoint mapping and referenced object, validated cascading constraints and enabled enforcement triggers. |
| New target namespace | No target workspace rows in any KV/status/cache table, graph namespace or existing vector-family table. |
| New target vector tables | Three token/dimension table names satisfy PostgreSQL's identifier limit and are absent, including empty tables. |
| Legacy and other model vector tables | Every existing family table has supported structure; a globally empty base table is rejected because upstream initialization can drop it. |
| Shared migration hazards | Old cache keys/columns, incompatible timestamp/field/pipeline columns, old chunk vector columns and superseded scheduling indexes cause rejection. |

Other workspaces may contain data. Graph edge normalization in the pinned version
is scoped to the selected workspace and namespace; reversed edges in another
workspace do not by themselves reject a fresh target. Shared graph schema repair,
deduplication and orphan deletion are prevented by rejecting an incompatible
schema before Core initialization.

An existing target vector table is rejected even if empty: its initialization can
repair indexes or alter vector column types. An interrupted target needs the
future resumable-target audit, not reuse of this fresh-target inspection.

## Meaning of the result

`FreshTargetPreflightReport` records database/schema and inspected table identities,
workspace and intended vector table names. It is an observation of that database
snapshot. It is **not** an initialization permit, verified-member receipt,
strict-absence receipt or activation receipt.

Repeatable read cannot prevent a writer or DDL operation after inspection. The
production coordinator must hold admission closed, drain existing work and exclude
external writers/DDL across inspection and initialization. It must then verify the
actual initialized storage identities and audit all persisted document, chunk,
vector and graph data before activation. No such protection or complete rebuild
is provided by this module alone.

Initial installation into an empty database requires its own controlled schema
bootstrap. Missing or incompatible shared schema belongs to an explicit maintenance
or recovery operation; this inspector does not repair or delete it.

See the [complete rebuild contract](m5-workspace-rebuild-contract.md) and
[generation ledger boundary](core-generation-ledger.md).
