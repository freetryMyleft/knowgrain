"""Pinned Core 1.5.7 admission and strict shutdown proof.

This module does not reset shared Core state or replace the application-wide
coordinator's persistent restart latch. A failed proof requires a process restart.
"""

import asyncio
from dataclasses import dataclass
from functools import wraps
from typing import Any

STORAGE_ATTRIBUTES = (
    "full_docs", "text_chunks", "full_entities", "full_relations", "entity_chunks",
    "relation_chunks", "entities_vdb", "relationships_vdb", "chunks_vdb",
    "chunk_entity_relation_graph", "llm_response_cache", "doc_status",
)
VECTOR_ATTRIBUTES = ("entities_vdb", "relationships_vdb", "chunks_vdb")
_MISSING = object()


class CoreCallGate:
    """Synchronous admission/counting on the owning event loop; reads stay parallel."""

    def __init__(self) -> None:
        self.closed = False
        self.count = 0
        self.drained = asyncio.Event()
        self.drained.set()
        self._tasks: dict[asyncio.Task, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("LightRAG Core gate requires its owning event loop")

    def enter(self) -> None:
        self._bind_loop()
        if self.closed:
            raise RuntimeError("LightRAG Core admission is closed")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("LightRAG Core call requires an asyncio task")
        self._tasks[task] = self._tasks.get(task, 0) + 1
        self.count += 1
        self.drained.clear()

    def exit(self) -> None:
        self._bind_loop()
        task = asyncio.current_task()
        if task not in self._tasks:
            raise RuntimeError("LightRAG Core admission exit has no matching call")
        self._tasks[task] -= 1
        if self._tasks[task] == 0:
            del self._tasks[task]
        self.count -= 1
        if self.count == 0:
            self.drained.set()

    def close(self) -> None:
        self._bind_loop()
        self.closed = True

    def open(self) -> None:
        self._bind_loop()
        if self.count:
            raise RuntimeError("Cannot reopen LightRAG Core while calls remain")
        self.closed = False

    def owns_current_task(self) -> bool:
        self._bind_loop()
        return asyncio.current_task() in self._tasks


def admitted_core_call(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        self._bind_event_loop()
        self._call_gate.enter()
        try:
            return await method(self, *args, **kwargs)
        finally:
            self._call_gate.exit()

    return guarded


@dataclass(frozen=True)
class CoreCloseProof:
    core_id: int
    epoch: int
    operations_drained: bool
    vectors_flushed: bool
    queues_drained: bool
    parser_joined: bool
    storages_finalized: bool
    resources_released: bool
    errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.errors and all((
            self.operations_drained, self.vectors_flushed, self.queues_drained,
            self.parser_joined, self.storages_finalized, self.resources_released,
        ))


async def close_core_strict(rag: Any, *, epoch: int, operations_drained: bool) -> CoreCloseProof:
    """Do not trust upstream finalize_storages(), which swallows finalizer failures.

    Capture resources before closing and require actual PG release, no vectors in
    pending buffers, no queue workers, and a joined parser executor. Missing pinned
    protocol fields fail closed rather than defaulting to an apparently empty state.
    """
    errors: list[str] = []
    flushed = queues = parser = finalized = released = False

    def proof() -> CoreCloseProof:
        return CoreCloseProof(id(rag), epoch, operations_drained, flushed, queues,
                              parser, finalized, released, tuple(errors))

    def failure(label: str, exc: BaseException | None = None) -> None:
        errors.append(label + (f" ({type(exc).__name__})" if exc is not None else ""))

    if not operations_drained:
        failure("operations not drained")
        return proof()
    stores: dict[str, Any] = {}
    pools: dict[int, Any] = {}
    for name in STORAGE_ATTRIBUTES:
        store = getattr(rag, name, _MISSING)
        if store is _MISSING or store is None or not callable(getattr(store, "finalize", None)):
            failure(name + " missing storage protocol")
            continue
        stores[name] = store
        db = getattr(store, "db", _MISSING)
        if db is _MISSING or db is None:
            failure(name + " missing initialized database")
            continue
        pool = getattr(db, "pool", _MISSING)
        if pool is _MISSING or pool is None:
            failure(name + " missing initialized pool")
        else:
            pools[id(pool)] = pool
    for name in VECTOR_ATTRIBUTES:
        store = stores.get(name)
        if (store is None or not callable(getattr(store, "_flush_pending_vector_ops", None))
                or type(getattr(store, "_pending_vector_docs", None)) is not dict
                or type(getattr(store, "_pending_vector_deletes", None)) is not set):
            failure(name + " missing vector protocol")
    role_funcs = getattr(rag, "role_llm_funcs", _MISSING)
    embed = getattr(getattr(rag, "embedding_func", None), "func", _MISSING)
    callbacks = []
    if not isinstance(role_funcs, dict) or not role_funcs or embed is _MISSING:
        failure("missing model queue registry")
    else:
        seen: set[int] = set()
        for callback in [*role_funcs.values(), embed]:
            if id(callback) not in seen:
                seen.add(id(callback))
                callbacks.append(callback)
            if not callable(getattr(callback, "shutdown", None)) or not callable(
                getattr(callback, "get_queue_stats", None)
            ):
                failure("missing model queue shutdown protocol")
    shutdown_parser = getattr(rag, "_shutdown_parser_executor", None)
    executor = getattr(rag, "_parser_executor", _MISSING)
    event = getattr(rag, "_parser_shutdown_event", _MISSING)
    if (not callable(shutdown_parser) or executor is _MISSING or event is _MISSING
            or not callable(getattr(event, "is_set", None))
            or not callable(getattr(event, "set", None))):
        failure("missing parser shutdown protocol")
    if executor is not None and executor is not _MISSING and (
        not callable(getattr(executor, "shutdown", None))
        or not isinstance(getattr(executor, "_threads", None), set)
    ):
        failure("missing parser executor join protocol")
    from lightrag.base import StoragesStatus
    from lightrag.llm_roles import ROLE_NAMES
    from lightrag.kg.postgres_impl import ClientManager

    if getattr(rag, "_storages_status", _MISSING) is not StoragesStatus.INITIALIZED:
        failure("Core storage status is not initialized")
    registry = ClientManager._instances
    if not isinstance(registry, dict) or not {"db", "ref_count", "vector_signature"} <= registry.keys():
        failure("missing PostgreSQL client registry")
    if isinstance(role_funcs, dict) and set(role_funcs) != ROLE_NAMES:
        failure("model queue registry does not contain all pinned roles")
    if len({id(store) for store in stores.values()}) != len(STORAGE_ATTRIBUTES):
        failure("storage objects are missing or not distinct")
    if isinstance(registry, dict):
        if type(registry.get("ref_count")) is not int or registry["ref_count"] != 12:
            failure("PostgreSQL client ownership count is not twelve")
        active_db = registry.get("db", _MISSING)
        if active_db is _MISSING or active_db is None or any(
            getattr(store, "db", _MISSING) is not active_db for store in stores.values()
        ):
            failure("storage database does not match PostgreSQL client ownership")
        signature = registry.get("vector_signature", _MISSING)
        if (not isinstance(signature, dict)
                or type(signature.get("enable_vector")) is not bool
                or "vector_storage" not in signature
                or (signature["vector_storage"] is not None
                    and not isinstance(signature["vector_storage"], str))):
            failure("PostgreSQL vector signature missing or malformed")
    for pool in pools.values():
        if type(getattr(pool, "_holders", _MISSING)) is not list:
            failure("PostgreSQL pool holder protocol missing")
    if errors:
        return proof()

    # Lazy vector writes can invoke the embedding queue, so flush before shutdown.
    for name in VECTOR_ATTRIBUTES:
        try:
            await stores[name]._flush_pending_vector_ops()
            if stores[name]._pending_vector_docs or stores[name]._pending_vector_deletes:
                failure(name + " vector flush left pending buffers")
        except Exception as exc:
            failure(name + " vector flush failed", exc)
    flushed = not errors
    # Shut down all callbacks even after a flush failure, but do not release stores.
    for callback in callbacks:
        try:
            await callback.shutdown(graceful=False)
            stats = await callback.get_queue_stats()
            if not isinstance(stats, dict) or any(
                type(stats.get(key)) is not int or stats[key] != 0
                for key in ("queued", "running", "in_flight", "worker_count")
            ) or stats.get("initialized") is not False:
                failure("model queue still active or stats missing")
        except Exception as exc:
            failure("model queue shutdown failed", exc)
    queues = not any(error.startswith("model queue") for error in errors)
    if errors:
        return proof()
    try:
        shutdown_parser()
        if not event.is_set() or rag._parser_executor is not None:
            raise RuntimeError("Parser protocol did not stop executor")
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
            if any(thread.is_alive() for thread in executor._threads):
                raise RuntimeError("Parser threads remain alive")
        parser = True
    except Exception as exc:
        failure("parser join failed", exc)
        return proof()

    for name, store in stores.items():
        try:
            await store.finalize()
        except Exception as exc:
            failure(name + " finalizer failed", exc)
    finalized = not errors
    for name, store in stores.items():
        if getattr(store, "db", _MISSING) is not None:
            failure(name + " database reference remains")
    for name in VECTOR_ATTRIBUTES:
        store = stores[name]
        if (type(getattr(store, "_pending_vector_docs", None)) is not dict
                or type(getattr(store, "_pending_vector_deletes", None)) is not set
                or store._pending_vector_docs or store._pending_vector_deletes):
            failure(name + " pending vector buffers remain")
    registry = ClientManager._instances
    if (not isinstance(registry, dict) or registry.get("db", _MISSING) is not None
            or type(registry.get("ref_count")) is not int or registry["ref_count"] != 0
            or registry.get("vector_signature", _MISSING) is not None):
        failure("PostgreSQL client references remain or registry missing")
    for pool in pools.values():
        if getattr(pool, "_closed", _MISSING) is not True or getattr(
            pool, "_closing", _MISSING
        ) is not False:
            failure("PostgreSQL pool is not fully closed")
        holders = getattr(pool, "_holders", _MISSING)
        if type(holders) is not list:
            failure("PostgreSQL pool holder protocol missing")
            continue
        for holder in holders:
            connection = getattr(holder, "_con", _MISSING)
            if connection is _MISSING or (connection is not None and (
                not callable(getattr(connection, "is_closed", None)) or not connection.is_closed()
            )):
                failure("PostgreSQL pool holder connection remains")
    released = not any(
        "reference" in error or "buffers" in error or "PostgreSQL" in error for error in errors
    )
    if not errors:
        rag._storages_status = StoragesStatus.FINALIZED
    return proof()
