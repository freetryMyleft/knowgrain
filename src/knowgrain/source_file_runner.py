"""Background executor for durable source archive and restore operations."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from collections.abc import Callable

from knowgrain.database import ApplicationDatabase
from knowgrain.source_file_service import SourceFileService
from knowgrain.source_file_repository import SourceFileRepository

logger = logging.getLogger(__name__)
_POLL_SECONDS = 1
_SCAN_INTERVAL_SECONDS = 30


class SourceFileRunner:
    """Drain source file operations when the application DB and Vault are ready."""

    def __init__(
        self,
        database: ApplicationDatabase,
        repository: SourceFileRepository,
        service: SourceFileService,
        *,
        vault_ready: Callable[[], bool],
    ) -> None:
        self.database = database
        self.repository = repository
        self.service = service
        self.vault_ready = vault_ready
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="knowgrain-source-file-operations"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # _run_next drains its filesystem thread before stop can get here.
        # SourceFileService takes the shared lock before releasing the owner.
        if self.database.is_ready:
            await self.service.stop()

    async def _run(self) -> None:
        next_scan = 0.0
        while True:
            if not self._dependencies_ready():
                await asyncio.sleep(_POLL_SECONDS)
                continue
            now = asyncio.get_running_loop().time()
            if now >= next_scan:
                try:
                    # Recovery scan is deliberately bounded and independent of
                    # LightRAG/Ollama readiness.
                    await self.repository.enqueue_cleaned_sources(limit=100)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Source archive recovery scan unavailable (%s)",
                        type(exc).__name__,
                    )
                next_scan = now + _SCAN_INTERVAL_SECONDS
            try:
                claimed = await self.service.run_next()
                if claimed:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Source file operation executor unavailable (%s)",
                    type(exc).__name__,
                )
            await asyncio.sleep(_POLL_SECONDS)

    def _dependencies_ready(self) -> bool:
        if not self.database.is_ready:
            return False
        try:
            return self.vault_ready() is True
        except Exception:
            return False
