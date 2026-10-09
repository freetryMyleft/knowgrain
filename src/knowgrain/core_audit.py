"""Direct, read-only observations of sealed LightRAG 1.5.7 PostgreSQL data.

No Core getters, model calls, flush, ledger mutation or activation authorization.
The caller must flush its drained Core and retain writer/DDL exclusion throughout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from .core_content_manifest import ExpectedContentManifest, _verify_implementation
from .core_preflight import ResolvedPostgresConfig, _column_spec, _pinned_ddls
from .index_identity import CoreIndexIdentity
from .index_profile import IndexProfile

_VECTOR_BASES = ("lightrag_vdb_entity", "lightrag_vdb_relation", "lightrag_vdb_chunks")
_TABLES = (
    "lightrag_doc_full",
    "lightrag_doc_status",
    "lightrag_doc_chunks",
    "lightrag_full_entities",
    "lightrag_full_relations",
    "lightrag_entity_chunks",
    "lightrag_relation_chunks",
    "lightrag_graph_nodes",
    "lightrag_graph_edges",
    "lightrag_llm_cache",
)
_GRAPH_NAMESPACE = "chunk_entity_relation"
_SEP = "<SEP>"


class AuditError(RuntimeError):
    """Static diagnostics only: never PostgreSQL errors, paths or stored content."""


@dataclass(frozen=True)
class AuditIssue:
    severity: str
    category: str
    message: str
    details: dict[str, int] | None = None


@dataclass(frozen=True)
class AuditReport:
    """Single-member observation; does not certify a workspace or activation."""

    passed: bool
    manifest_hash: str
    issues: tuple[AuditIssue, ...]
    summary: str


@dataclass(frozen=True)
class WorkspaceAuditReport:
    """Exact workspace observation, still not an activation/member receipt."""

    passed: bool
    manifest_hashes: tuple[str, ...]
    issues: tuple[AuditIssue, ...]
    summary: str


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _vector_table_names(identity: CoreIndexIdentity, dimension: int) -> dict[str, str]:
    suffix = (
        "" if identity.vector_model_name is None else f"_{identity.vector_model_name}_{dimension}d"
    )
    names = dict(zip(("entity", "relation", "chunks"), (base + suffix for base in _VECTOR_BASES)))
    if any(len(name.encode("utf-8")) > 63 for name in names.values()):
        raise AuditError("Vector table identity exceeds PostgreSQL limits")
    return names


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _ids(value: Any) -> list[str] | None:
    value = _json(value)
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v for v in value):
        return None
    result = list(value)
    return result if len(set(result)) == len(result) else None


def _pair(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(not isinstance(v, str) or not v or _SEP in v for v in value):
        return None
    return tuple(sorted(value))


def _issue(issues: list[AuditIssue], category: str, message: str, **counts: int) -> None:
    # IDs, entities, paths and property values may themselves contain private text.
    issues.append(AuditIssue("error", category, message, counts or None))


def _expect_set(
    issues: list[AuditIssue], category: str, label: str, actual: set, expected: set
) -> None:
    if actual != expected:
        _issue(issues, category, label, expected_count=len(expected), actual_count=len(actual))


def _prepare(
    manifests: Sequence[ExpectedContentManifest],
    config: ResolvedPostgresConfig,
    identity: CoreIndexIdentity,
    tokenizer: Any,
    cache: Path | None,
) -> dict[str, Any]:
    if not isinstance(config, ResolvedPostgresConfig) or not isinstance(
        identity, CoreIndexIdentity
    ):
        raise AuditError("Invalid audit connection or index identity")
    if config.workspace and config.workspace != identity.workspace:
        raise AuditError("Resolved PostgreSQL workspace differs from index identity")
    if not manifests or any(not isinstance(m, ExpectedContentManifest) for m in manifests):
        raise AuditError("Audit requires a nonempty expected manifest sequence")
    if tokenizer is None or not isinstance(cache, Path):
        raise AuditError("Audit requires a sealed tokenizer and local resource")
    if len({m.revision_id for m in manifests}) != len(manifests):
        raise AuditError("Audit requires unique revision manifests")
    try:
        profile = IndexProfile.from_canonical_json(manifests[0].profile_snapshot)
        payload = json.loads(profile.to_canonical_json())
        for manifest in manifests:
            if (
                manifest.profile_snapshot != profile.to_canonical_json()
                or manifest.profile_snapshot_fingerprint != profile.snapshot_fingerprint
                or manifest.content_fingerprint != profile.content_embedding_fingerprint
            ):
                raise AuditError("Expected manifest sealed profile mismatch")
            if (
                hashlib.sha256(manifest.core_text.encode()).hexdigest() != manifest.core_sha256
                or any(c.full_doc_id != str(manifest.revision_id) for c in manifest.chunks)
                or [c.chunk_order_index for c in manifest.chunks]
                != list(range(len(manifest.chunks)))
                or len({c.id for c in manifest.chunks}) != len(manifest.chunks)
            ):
                raise AuditError("Expected manifest content or ownership is invalid")
        _verify_implementation(payload, tokenizer, cache)
        _pinned_ddls()
    except AuditError:
        raise
    except Exception:  # noqa: BLE001 -- discard content-bearing diagnostics
        raise AuditError("Sealed audit implementation or tokenizer verification failed") from None
    return payload


async def _inspect_schema(
    conn: asyncpg.Connection, config: ResolvedPostgresConfig, names: dict[str, str], dimension: int
) -> list[str]:
    """Validate persisted schemas, rather than E1's fresh/empty target condition."""
    state = await conn.fetchrow("""SELECT current_database() AS db, current_schemas(false) AS schemas,
        current_schemas(true) AS all_schemas, current_setting('server_encoding') AS encoding,
        current_setting('session_replication_role') AS role""")
    if (
        state["db"] != config.database
        or state["schemas"] != ["public"]
        or set(state["all_schemas"]) != {"public", "pg_catalog"}
        or state["encoding"] != "UTF8"
        or state["role"] != "origin"
    ):
        raise AuditError("Audit database or schema identity is unsupported")
    types = await conn.fetch("""SELECT t.oid,t.typname,n.nspname,
        EXISTS(SELECT 1 FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_extension e ON e.oid=d.refobjid
        WHERE d.classid='pg_catalog.pg_type'::regclass AND d.objid=t.oid
        AND d.refclassid='pg_catalog.pg_extension'::regclass AND d.deptype='e'
        AND e.extname='vector') AS member FROM pg_catalog.pg_type t
        JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace WHERE t.typname IN ('vector','halfvec')""")
    if len(types) != 2 or any(not r["member"] or r["nspname"] != "public" for r in types):
        raise AuditError("Audit requires actual public pgvector extension types")
    vector_type = "halfvec" if config.vector_index_type == "HNSW_HALFVEC" else "vector"
    vector_oid = next(r["oid"] for r in types if r["typname"] == vector_type)
    objects = await conn.fetch("""SELECT c.oid,c.relname,n.nspname,c.relkind::text AS kind,
        c.relpersistence::text AS persistence,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE lower(c.relname) LIKE '%lightrag%' AND c.relkind NOT IN ('i','I')""")
    if any(r["nspname"] != "public" for r in objects):
        raise AuditError("Ambiguous LightRAG objects outside public schema")
    objects = {r["relname"]: r for r in objects}
    other_vectors = [
        name
        for name in objects
        if name not in names.values()
        and any(name == b or name.startswith(b + "_") for b in _VECTOR_BASES)
    ]
    ddls = _pinned_ddls()
    specs = {name: _column_spec(ddls[name]) for name in _TABLES if "graph_" not in name}
    for kind, name in names.items():
        base = _VECTOR_BASES[("entity", "relation", "chunks").index(kind)]
        specs[name] = _column_spec(ddls[base])
    for name in other_vectors:
        base = next(b for b in _VECTOR_BASES if name == b or name.startswith(b + "_"))
        specs[name] = _column_spec(ddls[base])
    for name in ("lightrag_graph_nodes", "lightrag_graph_edges"):
        keys = ("id",) if name.endswith("nodes") else ("src_id", "tgt_id")
        specs[name] = {k: ("text", True) for k in ("workspace", "namespace", *keys)}
        specs[name].update(
            properties=("jsonb", True), updated_at=("timestamp with time zone", True)
        )
    for name, spec in specs.items():
        obj = objects.get(name)
        if (
            obj is None
            or obj["kind"] != "r"
            or obj["persistence"] != "p"
            or obj["relrowsecurity"]
            or obj["relforcerowsecurity"]
        ):
            raise AuditError("Unsupported or missing LightRAG audit table")
        columns = await conn.fetch(
            """SELECT attname,attnum,atttypid,atttypmod,attnotnull,
            pg_catalog.format_type(atttypid,atttypmod) AS type FROM pg_catalog.pg_attribute
            WHERE attrelid=$1 AND attnum>0 AND NOT attisdropped""",
            obj["oid"],
        )
        if {r["attname"] for r in columns} != set(spec):
            raise AuditError("Unsupported LightRAG audit columns")
        for col in columns:
            typ, nonnull = spec[col["attname"]]
            if typ == "vector":
                valid = any(r["oid"] == col["atttypid"] for r in types) and col["atttypmod"] > 0
                if name in names.values():
                    valid = (
                        valid and col["atttypid"] == vector_oid and col["atttypmod"] == dimension
                    )
            else:
                valid = typ == col["type"]
            if not valid or nonnull != bool(col["attnotnull"]):
                raise AuditError("Unsupported LightRAG audit column identity")
        attrs = {r["attname"]: r["attnum"] for r in columns}
        constraints = await conn.fetch(
            """SELECT oid,conname,contype::text AS kind,conkey,confkey,confrelid,
            confdeltype::text AS deltype,convalidated FROM pg_catalog.pg_constraint WHERE conrelid=$1""",
            obj["oid"],
        )
        keys = (
            ["workspace", "id"]
            if "graph_" not in name
            else ["workspace", "namespace"]
            + (["id"] if name.endswith("nodes") else ["src_id", "tgt_id"])
        )
        pks = [r for r in constraints if r["kind"] == "p"]
        if (
            len(pks) != 1
            or list(pks[0]["conkey"]) != [attrs[k] for k in keys]
            or not pks[0]["convalidated"]
        ):
            raise AuditError("Unsupported LightRAG audit primary key")
        if name.endswith("graph_edges"):
            node = objects["lightrag_graph_nodes"]
            node_attrs = {
                r["attname"]: r["attnum"]
                for r in await conn.fetch(
                    "SELECT attname,attnum FROM pg_catalog.pg_attribute WHERE attrelid=$1",
                    node["oid"],
                )
            }
            for end in ("src", "tgt"):
                fk = next(
                    (r for r in constraints if r["conname"] == f"fk_lightrag_graph_edges_{end}"),
                    None,
                )
                if (
                    fk is None
                    or fk["kind"] != "f"
                    or not fk["convalidated"]
                    or fk["deltype"] != "c"
                    or fk["confrelid"] != node["oid"]
                    or list(fk["conkey"])
                    != [attrs[k] for k in ("workspace", "namespace", end + "_id")]
                    or list(fk["confkey"])
                    != [node_attrs[k] for k in ("workspace", "namespace", "id")]
                ):
                    raise AuditError("Unsupported LightRAG audit endpoint constraint")
                triggers = await conn.fetch(
                    "SELECT tgenabled::text AS enabled FROM pg_catalog.pg_trigger WHERE tgconstraint=$1",
                    fk["oid"],
                )
                if len(triggers) != 4 or any(r["enabled"] not in ("O", "A") for r in triggers):
                    raise AuditError("Audit graph endpoint enforcement is disabled")
    return other_vectors


async def _audit_connection(
    config: ResolvedPostgresConfig, identity: CoreIndexIdentity, dimension: int
) -> dict[str, list[dict[str, Any]]]:
    names = _vector_table_names(identity, dimension)
    conn = None
    cancellation = None
    try:
        async with asyncio.timeout(config.timeout):
            conn = await asyncpg.connect(**config.connect_kwargs())
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                others = await _inspect_schema(conn, config, names, dimension)
                queries = {
                    "lightrag_doc_full": "id,doc_name,content,content_hash,parse_format,meta,process_options,chunk_options,parse_engine,sidecar_location",
                    "lightrag_doc_status": "id,status,content_length,chunks_count,file_path,chunks_list,content_hash,metadata,error_msg",
                    "lightrag_doc_chunks": "id,full_doc_id,chunk_order_index,tokens,content,file_path,llm_cache_list,heading,sidecar",
                    "lightrag_full_entities": "id,entity_names,count",
                    "lightrag_full_relations": "id,relation_pairs,count",
                    "lightrag_entity_chunks": "id,chunk_ids,count",
                    "lightrag_relation_chunks": "id,chunk_ids,count",
                    "lightrag_graph_nodes": "namespace,id,properties",
                    "lightrag_graph_edges": "namespace,src_id,tgt_id,properties",
                    "lightrag_llm_cache": "id,chunk_id,cache_type",
                    names[
                        "chunks"
                    ]: "id,full_doc_id,chunk_order_index,tokens,content,file_path,content_vector::text AS vector_text",
                    names[
                        "entity"
                    ]: "id,entity_name,content,file_path,chunk_ids,content_vector::text AS vector_text",
                    names[
                        "relation"
                    ]: "id,source_id,target_id,content,file_path,chunk_ids,content_vector::text AS vector_text",
                }
                rows = {}
                for name, columns in queries.items():
                    records = await conn.fetch(
                        f"SELECT {columns} FROM public.{_q(name)} WHERE workspace=$1",
                        identity.workspace,
                    )
                    rows[name] = [dict(r) for r in records]
                for name in others:
                    if await conn.fetchval(
                        f"SELECT EXISTS(SELECT 1 FROM public.{_q(name)} WHERE workspace=$1)",
                        identity.workspace,
                    ):
                        raise AuditError(
                            "Unexpected vector model namespace contains target workspace data"
                        )
                return rows
    except asyncio.CancelledError as exc:
        cancellation = exc
        raise
    except AuditError:
        raise
    except Exception:  # noqa: BLE001 -- discard content-bearing diagnostics
        raise AuditError("PostgreSQL persisted-content audit failed") from None
    finally:
        if conn is not None:
            close = asyncio.create_task(conn.close(timeout=config.timeout))
            while not close.done():
                try:
                    await asyncio.shield(close)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except Exception:  # noqa: BLE001 -- discard content-bearing diagnostics
                    break
            try:
                close.result()
            except BaseException:  # noqa: BLE001 -- terminate even if cleanup is cancelled
                conn.terminate()
                if cancellation is not None:
                    raise cancellation
                raise AuditError("PostgreSQL audit connection cleanup failed") from None
            if cancellation is not None:
                raise cancellation


def _row_map(
    rows: list[dict[str, Any]], issues: list[AuditIssue], category: str
) -> dict[str, dict]:
    result = {}
    for row in rows:
        key = row.get("id")
        if not isinstance(key, str) or not key or key in result:
            _issue(issues, category, "Malformed or duplicate persisted identity")
        else:
            result[key] = row
    return result


def _vectors(rows: list[dict], dimension: int, issues: list[AuditIssue]) -> None:
    for row in rows:
        raw = row.get("vector_text")
        try:
            if not isinstance(raw, str) or not raw.startswith("[") or not raw.endswith("]"):
                raise ValueError
            values = [float(v) for v in raw[1:-1].split(",")]
            if len(values) != dimension or any(not math.isfinite(v) for v in values):
                raise ValueError
        except (ValueError, TypeError):
            _issue(
                issues,
                "vector",
                "Persisted vector is missing, malformed, non-finite or has the wrong dimension",
            )


def _validate_member(
    manifest: ExpectedContentManifest,
    rows: dict[str, list[dict]],
    names: dict[str, str],
    issues: list[AuditIssue],
) -> None:
    rid = str(manifest.revision_id)
    doc = next((r for r in rows["lightrag_doc_full"] if r.get("id") == rid), None)
    status = next((r for r in rows["lightrag_doc_status"] if r.get("id") == rid), None)
    expected_ids = [c.id for c in manifest.chunks]
    if doc is None:
        _issue(issues, "document", "Expected document is missing")
    elif (
        doc.get("content") != manifest.core_text
        or doc.get("content_hash") != manifest.core_sha256
        or doc.get("doc_name") != manifest.canonical_file_path
        or doc.get("parse_format") != manifest.raw_format
        or _json(doc.get("chunk_options")) != json.loads(manifest.chunk_options)
        or doc.get("process_options") not in (None, "")
        or doc.get("parse_engine") not in (None, "")
        or doc.get("sidecar_location") not in (None, "")
        or doc.get("meta") not in (None, {}, "{}")
    ):
        _issue(issues, "document", "Document content, hash or RAW metadata differs from E2")
    metadata = _json(status.get("metadata")) if status else None
    if (
        status is None
        or status.get("status") != "processed"
        or status.get("content_hash") != manifest.core_sha256
        or status.get("content_length") != len(manifest.core_text)
        or status.get("file_path") != manifest.canonical_file_path
        or status.get("error_msg") not in (None, "")
        or not isinstance(metadata, dict)
        or metadata.get("parse_format") != manifest.raw_format
        or metadata.get("is_duplicate", False) is not False
    ):
        _issue(issues, "document", "Processed status or document metadata differs from E2")
    if (
        status is None
        or _ids(status.get("chunks_list")) != expected_ids
        or status.get("chunks_count") != len(expected_ids)
    ):
        _issue(issues, "chunk", "Status chunk order or count differs from E2")
    expected = {c.id: c for c in manifest.chunks}
    for table, category in (("lightrag_doc_chunks", "chunk"), (names["chunks"], "vector")):
        actual = [r for r in rows[table] if r.get("full_doc_id") == rid]
        _expect_set(
            issues,
            category,
            "Member chunk identities differ from E2",
            {r.get("id") for r in actual},
            set(expected),
        )
        for row in actual:
            chunk = expected.get(row.get("id"))
            if chunk is None:
                continue
            if any(
                row.get(key) != value
                for key, value in (
                    ("content", chunk.content),
                    ("tokens", chunk.tokens),
                    ("chunk_order_index", chunk.chunk_order_index),
                    ("full_doc_id", chunk.full_doc_id),
                    ("file_path", chunk.file_path),
                )
            ):
                _issue(
                    issues,
                    category,
                    "Persisted chunk content, order, tokens or ownership differs from E2",
                )
            if category == "chunk" and (
                _json(row.get("heading")) != {} or _json(row.get("sidecar")) != {}
            ):
                _issue(issues, "chunk", "RAW chunk has unexpected structured metadata")


def _validate_file_path_membership(
    file_path: Any,
    tracking_ids: list[str],
    chunks: dict[str, dict],
    knobs: dict[str, Any],
    issues: list[AuditIssue],
) -> None:
    """Validate source membership without pretending to replay historical merges.

    Fresh merge and surviving-chunk rebuild use different placeholder forms.
    Tracking alone cannot reconstruct the history or retained path order.
    """
    candidates = set()
    for chunk_id in tracking_ids:
        path = chunks.get(chunk_id, {}).get("file_path")
        if not isinstance(path, str) or not path or _SEP in path:
            _issue(issues, "graph", "Tracking chunk source path is missing or ambiguous")
            return
        candidates.add(path)
    if not isinstance(file_path, str) or not file_path.strip():
        _issue(issues, "graph", "Graph source path metadata is missing")
        return
    parts = file_path.split(_SEP)
    limit = knobs["max_file_paths"]
    method = knobs["source_ids_limit_method"]
    placeholder = knobs["file_path_more_placeholder"]
    prefix = f"...{placeholder}..."
    legacy_marker = prefix + ("(KEEP Old)" if method == "KEEP" else "(FIFO)")
    # Counts describe the historical rebuild, not today's candidate cardinality.
    count_pattern = re.compile(re.escape(f"{prefix}({method} {limit}/") + r"([1-9][0-9]*)\)")

    def known_marker(value: str) -> bool:
        if value == legacy_marker:
            return True
        match = count_pattern.fullmatch(value)
        if match is None:
            return False
        denominator = match.group(1)
        numerator = str(limit)
        # Compare canonical decimal strings without parsing unbounded integers.
        return len(denominator) > len(numerator) or (
            len(denominator) == len(numerator) and denominator > numerator
        )

    retained = [part for part in parts if part in candidates]
    markers = [part for part in parts if part not in candidates]
    valid = (
        len(retained) <= limit
        and len(retained) == len(set(retained))
        and (bool(retained) or limit == 0)
        and (
            not markers
            or (len(markers) == 1 and known_marker(markers[0]) and parts[-1] == markers[0])
        )
    )
    if not valid:
        _issue(
            issues,
            "graph",
            "Graph source paths or truncation marker disagree with tracking sources",
        )


def _validate_graph(
    rows: dict[str, list[dict]],
    names: dict[str, str],
    payload: dict,
    tokenizer: Any,
    issues: list[AuditIssue],
    docs: dict,
    chunks: dict,
) -> None:
    anchors = {}
    relation_anchors = {}
    for table, field, dest in (
        ("lightrag_full_entities", "entity_names", anchors),
        ("lightrag_full_relations", "relation_pairs", relation_anchors),
    ):
        mapped = _row_map(rows[table], issues, "graph")
        _expect_set(
            issues, "graph", "Recovery anchor document identities differ", set(mapped), set(docs)
        )
        for rid, row in mapped.items():
            if field == "entity_names":
                values = _ids(row.get(field))
            else:
                raw = _json(row.get(field))
                values = [_pair(v) for v in raw] if isinstance(raw, list) else None
                if values is not None and (None in values or len(set(values)) != len(values)):
                    values = None
            if values is None or row.get("count") != len(values):
                _issue(issues, "graph", "Recovery anchor list or count is malformed")
                dest[rid] = set()
            else:
                dest[rid] = set(values)
    nodes = _row_map(rows["lightrag_graph_nodes"], issues, "graph")
    edges = {}
    for row in rows["lightrag_graph_nodes"] + rows["lightrag_graph_edges"]:
        if row.get("namespace") != _GRAPH_NAMESPACE:
            _issue(issues, "namespace", "Unexpected graph namespace in target workspace")
    for row in rows["lightrag_graph_edges"]:
        pair = _pair((row.get("src_id"), row.get("tgt_id")))
        if pair is None or pair in edges or pair != (row.get("src_id"), row.get("tgt_id")):
            _issue(issues, "graph", "Malformed, reversed or duplicate graph edge")
            continue
        edges[pair] = row
        if not set(pair) <= set(nodes):
            _issue(issues, "graph", "Graph relation endpoint is missing")
    union_entities = set().union(*anchors.values()) if anchors else set()
    union_relations = set().union(*relation_anchors.values()) if relation_anchors else set()
    _expect_set(
        issues, "graph", "Graph node and document anchor union differ", set(nodes), union_entities
    )
    _expect_set(
        issues, "graph", "Graph edge and document anchor union differ", set(edges), union_relations
    )
    for kind, graph, per_document in (
        ("entity", nodes, anchors),
        ("relation", edges, relation_anchors),
    ):
        tracking = {}
        for row in rows[f"lightrag_{kind}_chunks"]:
            key = row.get("id")
            if kind == "relation":
                parts = key.split(_SEP) if isinstance(key, str) else []
                key = _pair(parts)
                if key is not None and list(key) != parts:
                    key = None
            if key is None or key in tracking:
                _issue(issues, "graph", "Malformed or duplicate graph tracking identity")
                continue
            values = _ids(row.get("chunk_ids"))
            if (
                values is None
                or not values
                or row.get("count") != len(values)
                or not set(values) <= set(chunks)
            ):
                _issue(issues, "graph", "Graph tracking chunk list, count or ownership is invalid")
                values = []
            tracking[key] = values
        _expect_set(
            issues, "graph", "Graph and tracking identities differ", set(graph), set(tracking)
        )
        vectors = {}
        for row in rows[names[kind]]:
            key = (
                row.get("entity_name")
                if kind == "entity"
                else _pair((row.get("source_id"), row.get("target_id")))
            )
            if not isinstance(key, (str, tuple)) or not key or key in vectors:
                _issue(issues, "vector", "Malformed or duplicate graph vector identity")
                continue
            vectors[key] = row
        _expect_set(issues, "graph", "Graph and vector identities differ", set(graph), set(vectors))
        for key, item in graph.items():
            values = tracking.get(key, [])
            owners = {chunks[c]["full_doc_id"] for c in values if c in chunks}
            anchored_owners = {rid for rid, candidates in per_document.items() if key in candidates}
            _expect_set(
                issues,
                "graph",
                "Graph tracking owners and document anchors differ",
                owners,
                anchored_owners,
            )
            props = _json(item.get("properties"))
            if not isinstance(props, dict):
                _issue(issues, "graph", "Graph properties are malformed")
                continue
            knobs = payload["graph"]["knobs"]
            limit = knobs[f"max_source_ids_per_{kind}"]
            projection = (
                values
                if len(values) <= limit
                else values[-limit:]
                if knobs["source_ids_limit_method"] == "FIFO" and limit > 0
                else values[:limit]
            )
            if limit <= 0:
                projection = []
            if props.get("source_id") != _SEP.join(projection):
                _issue(
                    issues,
                    "graph",
                    "Graph provenance differs from sealed capped tracking projection",
                )
            _validate_file_path_membership(props.get("file_path"), values, chunks, knobs, issues)
            description = props.get("description")
            if not isinstance(description, str) or not isinstance(props.get("file_path"), str):
                _issue(issues, "graph", "Graph description or file metadata is malformed")
                continue
            if kind == "entity":
                if props.get("entity_id") != key or not isinstance(props.get("entity_type"), str):
                    _issue(issues, "graph", "Graph entity metadata is malformed")
                content = f"{key}\n{description}"
                vector_id = "ent-" + hashlib.md5(key.encode()).hexdigest()
            else:
                if (
                    not isinstance(props.get("keywords"), str)
                    or not isinstance(props.get("weight"), (int, float))
                    or not math.isfinite(props["weight"])
                ):
                    _issue(issues, "graph", "Graph relation metadata is malformed")
                    continue
                content = f"{props['keywords']}\t{key[0]}\n{key[1]}\n{description}"
                vector_id = "rel-" + hashlib.md5((key[0] + key[1]).encode()).hexdigest()
            span = tokenizer.truncate_by_token_limit(
                content, payload["content"]["limits"]["embedding_token_limit"]
            )
            content = content[span.start : span.end]
            vector = vectors.get(key)
            if vector is not None and (
                vector.get("id") != vector_id
                or vector.get("content") != content
                or _ids(vector.get("chunk_ids")) != projection
                or vector.get("file_path") != props["file_path"]
            ):
                _issue(
                    issues,
                    "vector",
                    "Graph vector indexed text, identity or provenance differs from graph",
                )


def _validate(
    rows: dict[str, list[dict]],
    manifests: Sequence[ExpectedContentManifest],
    identity: CoreIndexIdentity,
    payload: dict,
    tokenizer: Any,
    exact: bool,
) -> tuple[AuditIssue, ...]:
    issues = []
    dimension = payload["embedding"]["config"]["dimension"]
    names = _vector_table_names(identity, dimension)
    docs = _row_map(rows["lightrag_doc_full"], issues, "document")
    statuses = _row_map(rows["lightrag_doc_status"], issues, "document")
    chunks = _row_map(rows["lightrag_doc_chunks"], issues, "chunk")
    chunk_vectors = _row_map(rows[names["chunks"]], issues, "vector")
    for m in manifests:
        _validate_member(m, rows, names, issues)
    expected_docs = {str(m.revision_id) for m in manifests}
    expected_chunks = {c.id for m in manifests for c in m.chunks}
    if exact:
        for category, actual, expected in (
            ("document", set(docs), expected_docs),
            ("document", set(statuses), expected_docs),
            ("chunk", set(chunks), expected_chunks),
            ("vector", set(chunk_vectors), expected_chunks),
        ):
            _expect_set(
                issues,
                category,
                "Workspace persisted identities differ from complete expected manifests",
                actual,
                expected,
            )
    _expect_set(issues, "document", "Document and status sets differ", set(docs), set(statuses))
    _expect_set(
        issues, "vector", "Chunk text and vector sets differ", set(chunks), set(chunk_vectors)
    )
    for row in chunks.values():
        owner = row.get("full_doc_id")
        if (
            owner not in docs
            or owner not in statuses
            or statuses[owner].get("status") != "processed"
        ):
            _issue(issues, "chunk", "Persisted chunk has no processed document owner")
    cache = _row_map(rows["lightrag_llm_cache"], issues, "chunk")
    for row in chunks.values():
        cache_ids = _ids(row.get("llm_cache_list"))
        if cache_ids is None or any(
            key not in cache or cache[key].get("chunk_id") != row.get("id") for key in cache_ids
        ):
            _issue(issues, "chunk", "Runtime chunk cache references are malformed or dangling")
    for row in cache.values():
        if (
            not isinstance(row.get("id"), str)
            or ":" not in row["id"]
            or row.get("chunk_id") not in (None, "", *chunks)
        ):
            _issue(issues, "chunk", "Cache namespace or chunk reference is malformed")
    for name in names.values():
        _vectors(rows[name], dimension, issues)
    _validate_graph(rows, names, payload, tokenizer, issues, docs, chunks)
    return tuple(issues)


async def _audit(manifests, config, identity, tokenizer, cache, exact):
    payload = _prepare(manifests, config, identity, tokenizer, cache)
    rows = await _audit_connection(config, identity, payload["embedding"]["config"]["dimension"])
    try:
        return _validate(rows, manifests, identity, payload, tokenizer, exact)
    except Exception:  # noqa: BLE001 -- discard content-bearing diagnostics
        # Unknown row shapes/tokenizer failures never mean empty graph or success.
        raise AuditError("Persisted-content validation failed") from None


async def audit_persisted_content(
    manifest: ExpectedContentManifest,
    pg_config: ResolvedPostgresConfig,
    identity: CoreIndexIdentity,
    *,
    tokenizer: Any = None,
    tokenizer_cache_dir: Path | None = None,
) -> AuditReport:
    """Observe one revision plus workspace structural integrity, without activation."""
    issues = await _audit([manifest], pg_config, identity, tokenizer, tokenizer_cache_dir, False)
    passed = not issues
    return AuditReport(
        passed,
        manifest.digest,
        issues,
        "Member observation passed; workspace membership is not certified."
        if passed
        else f"Member audit found {len(issues)} error(s).",
    )


async def audit_persisted_workspace_content(
    manifests: Sequence[ExpectedContentManifest],
    pg_config: ResolvedPostgresConfig,
    identity: CoreIndexIdentity,
    *,
    tokenizer: Any = None,
    tokenizer_cache_dir: Path | None = None,
) -> WorkspaceAuditReport:
    """Observe exact contents for the caller's complete, retained source snapshot.

    The function cannot prove that the supplied sequence is the active/latest
    snapshot. It does not retain grants, flush Core, fence writers or activate.
    Empty workspaces require a separately specified empty-workspace contract.
    """
    manifests = tuple(manifests)
    issues = await _audit(manifests, pg_config, identity, tokenizer, tokenizer_cache_dir, True)
    passed = not issues
    return WorkspaceAuditReport(
        passed,
        tuple(m.digest for m in manifests),
        issues,
        "Workspace observation passed; activation is not authorized."
        if passed
        else f"Workspace audit found {len(issues)} error(s).",
    )
