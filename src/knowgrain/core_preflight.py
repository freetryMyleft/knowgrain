"""Read-only, pinned-Core schema inspection before a fresh workspace rebuild.

The report is a catalog snapshot, never an activation or initialization receipt.
The coordinator must still exclude writers and validate subsequent Core startup.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import distribution
from typing import Any

import asyncpg

from knowgrain.index_identity import CoreIndexIdentity


class PreflightError(RuntimeError):
    """A safe, static failure reason; never includes server errors or row content."""


@dataclass(frozen=True)
class ResolvedPostgresConfig:
    host: str
    port: int
    user: str
    database: str
    password: str | None = field(default=None, repr=False)
    ssl_mode: str | None = None
    statement_cache_size: int | None = None
    timeout: float = 15.0
    workspace: str | None = None
    vector_index_type: str = "HNSW"

    def __post_init__(self) -> None:
        if any(
            not isinstance(v, str) or not v.strip() for v in (self.host, self.user, self.database)
        ):
            raise PreflightError("Invalid resolved PostgreSQL connection identity")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise PreflightError("Invalid resolved PostgreSQL port")
        if self.vector_index_type not in ("HNSW", "HNSW_HALFVEC", "IVFFLAT"):
            raise PreflightError("Unsupported PostgreSQL vector index configuration")
        if self.ssl_mode not in (None, "disable"):
            raise PreflightError("Unsupported PostgreSQL SSL configuration")
        if self.workspace is not None and not isinstance(self.workspace, str):
            raise PreflightError("Invalid resolved PostgreSQL workspace")
        if self.password is not None and not isinstance(self.password, str):
            raise PreflightError("Invalid resolved PostgreSQL password")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (float, int))
            or not 0 < self.timeout <= 60
        ):
            raise PreflightError("Invalid PostgreSQL inspection timeout")
        if self.statement_cache_size is not None and (
            isinstance(self.statement_cache_size, bool)
            or not isinstance(self.statement_cache_size, int)
            or self.statement_cache_size < 0
        ):
            raise PreflightError("Invalid PostgreSQL statement cache size")

    @classmethod
    def from_upstream(cls, resolved: Mapping[str, Any]) -> ResolvedPostgresConfig:
        """Consume ClientManager.get_config's result without resolving environment again."""
        if resolved.get("enable_vector") is not True:
            raise PreflightError("Resolved PostgreSQL config must enable PGVector storage")
        if resolved.get("server_settings") not in (None, "", {}):
            raise PreflightError("Unsupported PostgreSQL certificate or server settings")
        if any(
            resolved.get(key) not in (None, "")
            for key in ("ssl_cert", "ssl_key", "ssl_root_cert", "ssl_crl")
        ):
            raise PreflightError("Unsupported PostgreSQL certificate or server settings")
        try:
            port = resolved["port"]
            cache = resolved.get("statement_cache_size")
            if (
                not isinstance(port, (str, int))
                or isinstance(port, bool)
                or (
                    cache is not None
                    and (not isinstance(cache, (str, int)) or isinstance(cache, bool))
                )
            ):
                raise TypeError
            return cls(
                host=resolved["host"],
                port=int(port),
                user=resolved["user"],
                database=resolved["database"],
                password=resolved.get("password"),
                ssl_mode=resolved.get("ssl_mode"),
                statement_cache_size=None if cache is None else int(cache),
                workspace=resolved.get("workspace"),
                vector_index_type=resolved["vector_index_type"],
            )
        except (KeyError, TypeError, ValueError):
            raise PreflightError("Invalid resolved PostgreSQL configuration") from None

    def connect_kwargs(self) -> dict[str, Any]:
        result = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "password": self.password,
            "timeout": self.timeout,
            "command_timeout": self.timeout,
        }
        # None deliberately omits ssl, just as pinned Core's pool bootstrap does.
        if self.ssl_mode == "disable":
            result["ssl"] = False
        if self.statement_cache_size is not None:
            result["statement_cache_size"] = self.statement_cache_size
        return result


@dataclass(frozen=True)
class InspectedTable:
    name: str
    oid: int


@dataclass(frozen=True)
class FreshTargetPreflightReport:
    database: str
    database_oid: int
    schema_oid: int
    workspace: str
    target_vector_tables: tuple[str, ...]
    inspected_tables: tuple[InspectedTable, ...]
    upstream_version: str = "1.5.7"


_SOURCE_HASHES = {
    "postgres_impl.py": "b3cf6f2b48a2d10da9a6254eacc0c1a8db7268b0dd41012f148c0df9c2eed2c4",
    "pgtable_impl.py": "d0658945379b7fd2a61dbefb14ae0d87837eaeee5282b2d21454f712a16c3699",
}
_VECTOR_BASES = ("lightrag_vdb_entity", "lightrag_vdb_relation", "lightrag_vdb_chunks")


def _pinned_ddls() -> dict[str, str]:
    package = distribution("lightrag-hku")
    if package.version != "1.5.7":
        raise PreflightError("Unsupported LightRAG storage implementation")
    sources = {}
    for name, digest in _SOURCE_HASHES.items():
        raw = package.locate_file(f"lightrag/kg/{name}").read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PreflightError("Unsupported LightRAG storage implementation")
        sources[name] = raw.decode("utf-8")
    for node in ast.parse(sources["postgres_impl.py"]).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TABLES" for target in node.targets
        ):
            tables = ast.literal_eval(node.value)
            return {name.lower(): entry["ddl"] for name, entry in tables.items()}
    raise PreflightError("Unsupported LightRAG storage implementation")


def target_vector_table_names(identity: CoreIndexIdentity, embedding_dim: int) -> tuple[str, ...]:
    if identity.vector_model_name is None:
        raise PreflightError("Fresh rebuild requires a vector model token")
    if isinstance(embedding_dim, bool) or not isinstance(embedding_dim, int) or embedding_dim <= 0:
        raise PreflightError("Invalid embedding dimension")
    names = tuple(f"{base}_{identity.vector_model_name}_{embedding_dim}d" for base in _VECTOR_BASES)
    if any(len(name.encode("utf-8")) > 63 for name in names):
        raise PreflightError("Target vector table name exceeds PostgreSQL identifier limit")
    return names


def _column_spec(ddl: str) -> dict[str, tuple[str, bool]]:
    ddl = re.sub(r"--[^\n]*", "", ddl)
    result = {}
    types = {
        "TEXT": "text",
        "JSONB": "jsonb",
        "INTEGER": "integer",
        "INT4": "integer",
        "TIMESTAMPTZ": "timestamp with time zone",
        "TIMESTAMP": "timestamp without time zone",
    }
    for match in re.finditer(
        r"^\s*(\w+)\s+(VARCHAR\(\d+\)(?:\[\])?|TIMESTAMPTZ|TIMESTAMP(?:\(0\))?|TEXT|JSONB|INTEGER|int4|VECTOR\(dimension\))([^,\n]*)",
        ddl,
        re.MULTILINE | re.IGNORECASE,
    ):
        name, raw, tail = match.groups()
        raw = raw.upper()
        if raw.startswith("VARCHAR"):
            typ = "character varying" + raw[7:]
        elif raw == "TIMESTAMP(0)":
            typ = "timestamp(0) without time zone"
        elif raw.startswith("VECTOR"):
            typ = "vector"
        else:
            typ = types[raw]
        result[name.lower()] = (
            typ,
            "NOT NULL" in tail.upper() or name.lower() in ("workspace", "id"),
        )
    return result


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _inspect(
    conn: asyncpg.Connection,
    identity: CoreIndexIdentity,
    targets: tuple[str, ...],
    ddls: dict[str, str],
) -> FreshTargetPreflightReport:
    state = await conn.fetchrow("""SELECT current_database() AS db,
        (SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()) AS db_oid,
        (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname='public') AS schema_oid,
        current_schemas(false) AS explicit_schemas, current_schemas(true) AS all_schemas,
        current_setting('server_encoding') AS encoding,
        current_setting('session_replication_role') AS replication_role""")
    if (
        not state["schema_oid"]
        or state["explicit_schemas"] != ["public"]
        or set(state["all_schemas"]) != {"public", "pg_catalog"}
        or state["encoding"] != "UTF8"
        or state["replication_role"] != "origin"
    ):
        raise PreflightError("Unsupported PostgreSQL schema resolution or encoding")
    types = await conn.fetch("""SELECT t.oid, t.typname, n.nspname,
        EXISTS(SELECT 1 FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_extension e
        ON e.oid=d.refobjid WHERE d.classid='pg_catalog.pg_type'::regclass
        AND d.objid=t.oid AND d.refclassid='pg_catalog.pg_extension'::regclass
        AND d.deptype='e' AND e.extname='vector') AS member
        FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
        WHERE t.typname IN ('vector','halfvec')""")
    if len(types) != 2 or any(not r["member"] or r["nspname"] != "public" for r in types):
        raise PreflightError("Required pgvector extension types are not ready")
    vector_oids = {r["oid"] for r in types}
    objects = await conn.fetch("""SELECT c.oid,c.relname,n.nspname,c.relkind::text AS relkind,c.relpersistence::text AS relpersistence,
        c.relrowsecurity,c.relforcerowsecurity FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE lower(c.relname) LIKE '%lightrag%'""")
    if any(r["nspname"] != "public" for r in objects):
        raise PreflightError("Ambiguous LightRAG objects outside public schema")
    tables = {r["relname"]: r for r in objects if r["relkind"] not in ("i", "I")}
    if any(name in {r["relname"] for r in objects} for name in targets):
        raise PreflightError("Target vector table already exists")
    if any(r["relname"] == "idx_lightrag_doc_status_ws_status_created_id" for r in objects):
        raise PreflightError("Superseded LightRAG scheduling index exists")
    required = {name: ddl for name, ddl in ddls.items() if name not in _VECTOR_BASES}
    graph_names = ("lightrag_graph_nodes", "lightrag_graph_edges")
    for name in graph_names:
        endpoint = (
            "id TEXT NOT NULL,"
            if name.endswith("nodes")
            else "src_id TEXT NOT NULL,\n tgt_id TEXT NOT NULL,"
        )
        required[name] = (
            f"workspace TEXT NOT NULL,\n namespace TEXT NOT NULL,\n {endpoint}\n properties JSONB NOT NULL,\n updated_at TIMESTAMPTZ NOT NULL"
        )
    vectors = {}
    for name in tables:
        if any(name == base or name.startswith(base + "_") for base in _VECTOR_BASES):
            base = next(b for b in _VECTOR_BASES if name == b or name.startswith(b + "_"))
            vectors[name] = ddls[base]
    inspected = []
    for name, ddl in (required | vectors).items():
        obj = tables.get(name)
        if obj is None:
            raise PreflightError("Required LightRAG table is missing")
        if (
            obj["relkind"] != "r"
            or obj["relpersistence"] != "p"
            or obj["relrowsecurity"]
            or obj["relforcerowsecurity"]
        ):
            raise PreflightError("Unsupported LightRAG table storage or row security")
        resolved = await conn.fetchval("SELECT pg_catalog.to_regclass($1)::oid", name)
        if resolved != obj["oid"]:
            raise PreflightError("Ambiguous LightRAG table resolution")
        columns = await conn.fetch(
            """SELECT attname,atttypid,atttypmod,attnotnull,
            pg_catalog.format_type(atttypid,atttypmod) AS type FROM pg_catalog.pg_attribute
            WHERE attrelid=$1 AND attnum>0 AND NOT attisdropped""",
            obj["oid"],
        )
        spec = _column_spec(ddl)
        if {r["attname"] for r in columns} != set(spec):
            raise PreflightError("LightRAG table columns require migration")
        for col in columns:
            typ, nonnull = spec[col["attname"]]
            if typ == "vector":
                valid = col["atttypid"] in vector_oids and col["atttypmod"] > 0
            else:
                valid = col["type"] == typ
            if not valid or bool(col["attnotnull"]) != nonnull:
                raise PreflightError("LightRAG table column type or nullability mismatch")
        constraints = await conn.fetch(
            """SELECT oid,conname,contype::text AS contype,conkey,confkey,confrelid,
            confdeltype::text AS confdeltype,convalidated FROM pg_catalog.pg_constraint WHERE conrelid=$1""",
            obj["oid"],
        )
        attrs = await conn.fetch(
            "SELECT attnum,attname FROM pg_catalog.pg_attribute WHERE attrelid=$1", obj["oid"]
        )
        numbers = {r["attname"]: r["attnum"] for r in attrs}
        keys = ["workspace", "id"]
        if name in graph_names:
            keys = ["workspace", "namespace"] + (
                ["id"] if name.endswith("nodes") else ["src_id", "tgt_id"]
            )
        pks = [r for r in constraints if r["contype"] == "p"]
        if (
            len(pks) != 1
            or list(pks[0]["conkey"]) != [numbers[k] for k in keys]
            or not pks[0]["convalidated"]
            or (name in graph_names and pks[0]["conname"] != name + "_pkey")
        ):
            raise PreflightError("LightRAG primary key mismatch")
        if name.endswith("graph_edges"):
            nodes_oid = tables["lightrag_graph_nodes"]["oid"]
            node_attrs = await conn.fetch(
                "SELECT attnum,attname FROM pg_catalog.pg_attribute WHERE attrelid=$1", nodes_oid
            )
            node_numbers = {r["attname"]: r["attnum"] for r in node_attrs}
            for end in ("src", "tgt"):
                fk = next(
                    (r for r in constraints if r["conname"] == f"fk_lightrag_graph_edges_{end}"),
                    None,
                )
                if (
                    fk is None
                    or fk["contype"] != "f"
                    or fk["confrelid"] != nodes_oid
                    or list(fk["conkey"])
                    != [numbers[k] for k in ("workspace", "namespace", end + "_id")]
                    or list(fk["confkey"])
                    != [node_numbers[k] for k in ("workspace", "namespace", "id")]
                    or fk["confdeltype"] != "c"
                    or not fk["convalidated"]
                ):
                    raise PreflightError("LightRAG graph foreign key mismatch")
                triggers = await conn.fetch(
                    "SELECT tgenabled::text AS tgenabled FROM pg_catalog.pg_trigger WHERE tgconstraint=$1",
                    fk["oid"],
                )
                if len(triggers) != 4 or any(t["tgenabled"] not in ("O", "A") for t in triggers):
                    raise PreflightError("LightRAG graph foreign key enforcement is disabled")
        if await conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM public.{_q(name)} WHERE workspace=$1)", identity.workspace
        ):
            raise PreflightError("Fresh workspace already contains LightRAG data")
        if name in _VECTOR_BASES and not await conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM public.{_q(name)})"
        ):
            raise PreflightError("Empty legacy vector table would be dropped by Core")
        if name == "lightrag_llm_cache" and await conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM public.{_q(name)} WHERE id NOT LIKE '%:%')"
        ):
            raise PreflightError("Legacy LightRAG cache keys require migration")
        inspected.append(InspectedTable(name, obj["oid"]))
    return FreshTargetPreflightReport(
        state["db"],
        state["db_oid"],
        state["schema_oid"],
        identity.workspace,
        targets,
        tuple(inspected),
    )


async def inspect_fresh_rebuild_target(
    config: ResolvedPostgresConfig, identity: CoreIndexIdentity, embedding_dim: int
) -> FreshTargetPreflightReport:
    """Inspect a committed database snapshot on a separate, bounded direct connection."""
    conn = None
    cancellation: asyncio.CancelledError | None = None
    try:
        if config.workspace and config.workspace != identity.workspace:
            raise PreflightError("Resolved PostgreSQL workspace differs from target identity")
        targets = target_vector_table_names(identity, embedding_dim)
        ddls = _pinned_ddls()
        async with asyncio.timeout(config.timeout):
            conn = await asyncpg.connect(**config.connect_kwargs())
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                return await _inspect(conn, identity, targets, ddls)
    except asyncio.CancelledError as exc:
        cancellation = exc
        raise
    except PreflightError:
        raise
    except Exception:  # noqa: BLE001 -- server diagnostics must stay private
        raise PreflightError("PostgreSQL fresh target inspection failed") from None
    finally:
        if conn is not None:
            # A caller cancellation must not strand a live inspection connection.
            close = asyncio.create_task(conn.close(timeout=config.timeout))
            while not close.done():
                try:
                    await asyncio.shield(close)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
                except Exception:  # noqa: BLE001 -- server diagnostics must stay private
                    break
            try:
                close.result()
            except BaseException:  # noqa: BLE001 -- terminate even if close is cancelled
                conn.terminate()
                if cancellation is not None:
                    raise cancellation
                raise PreflightError("PostgreSQL inspection connection cleanup failed") from None
            if cancellation is not None:
                raise cancellation
