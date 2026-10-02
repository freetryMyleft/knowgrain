"""Compare current application revisions with strict Core document reads."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import re
from uuid import UUID

from knowgrain.evidence_access import EvidenceAccess
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.source_repository import SourceRepository
from knowgrain.source_service import _thread_call_drained
from knowgrain.vault import VaultStore

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ReconciliationService:
    """Startup inspection; repair intents live in the existing index job ledger."""

    def __init__(
        self, repository: SourceRepository, core: LightRAGRuntime,
        vault: VaultStore, file_lock: asyncio.Lock,
    ) -> None:
        self.repository = repository
        self.core = core
        self.vault = vault
        self.file_lock = file_lock
        self.report = self._report("pending")

    @staticmethod
    def _report(state: str) -> dict:
        return {
            "state": state,
            "checked": 0,
            "healthy": 0,
            "repair_queued": 0,
            "skipped": 0,
            "finished_at": None,
            "detail": None,
        }

    async def run(self) -> dict:
        self.report = self._report("pending")
        try:
            # Startup/reinitialization also holds the runtime lock and stops
            # index writers. This lock joins file replay/import I/O before reads.
            async with self.file_lock:
                await self._scan()
        except BaseException:
            self.report["state"] = "unavailable"
            self.report["detail"] = "启动索引对账未完成；检查数据库与 Vault 后重新初始化"
            raise
        self.report["state"] = "complete"
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        return dict(self.report)

    async def _scan(self) -> None:
        cursor = None
        access = EvidenceAccess(self.vault)
        while True:
            candidates = await self.repository.list_reconciliation_candidates(
                after_source_id=cursor, limit=100,
            )
            if not isinstance(candidates, list) or len(candidates) > 100:
                raise RuntimeError("Reconciliation candidate batch is invalid")
            if not candidates:
                return
            for candidate in candidates:
                source_id = UUID(candidate["source_id"])
                if cursor is not None and source_id <= cursor:
                    raise RuntimeError("Reconciliation cursor did not advance")
                cursor = source_id
                await _thread_call_drained(
                    access.original_revision, candidate["vault_path"], candidate["sha256"],
                    allow_archived=False,
                )
                digest = candidate["parsed_text_sha256"]
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    state, chunk_ids = "invalid_metadata", ()
                else:
                    inspection = await self.core.inspect_revision(
                        source_id=candidate["revision_id"],
                        expected_text_sha256=digest,
                        expected_chunk_ids=candidate["cleanup_chunk_ids"],
                    )
                    if not isinstance(inspection, dict):
                        raise RuntimeError("Core inspection returned an invalid report")
                    state = inspection.get("state")
                    chunk_ids = inspection.get("chunk_ids")
                    if state not in {"healthy", "missing", "inconsistent"}:
                        raise RuntimeError("Core inspection returned an invalid state")
                    if not isinstance(chunk_ids, tuple):
                        raise RuntimeError("Core inspection returned an invalid manifest")
                self.report["checked"] += 1
                if state == "healthy":
                    self.report["healthy"] += 1
                    continue
                queued = await self.repository.queue_reconciliation_repair(
                    candidate, chunk_ids=chunk_ids, reason=state,
                )
                self.report["repair_queued" if queued is not None else "skipped"] += 1
