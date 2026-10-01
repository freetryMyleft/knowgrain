"""Coordinate authoritative Wiki files and their rebuildable DB projection."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from datetime import UTC, datetime
from uuid import UUID

from watchfiles import awatch

from knowgrain.vault import VaultStore
from knowgrain.wiki_files import (
    WikiConflictError,
    WikiFile,
    WikiFileStore,
    WikiNotFoundError,
    WikiScan,
)
from knowgrain.wiki_repository import WikiRepository


logger = logging.getLogger(__name__)


class WikiScanUnavailableError(RuntimeError):
    def __init__(self, scan: WikiScan) -> None:
        super().__init__("Wiki scan exceeded its safe bounds; no projection was replaced")
        self.scan = scan


class WikiService:
    """Serialize Web edits, file scans and projection replacement on one loop.

    Files remain authoritative if the DB write fails. The next successful scan
    rebuilds metadata from disk; a failed projection never causes file rollback.
    """

    def __init__(self, vault: VaultStore, repository: WikiRepository) -> None:
        self.files = WikiFileStore(vault)
        self.repository = repository
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self._projection_signature: tuple | None = None

    async def _scan_locked(self) -> WikiScan:
        scan = await self._file_call(self.files.scan)
        if not scan.complete or any(issue.code in {"scan_limit", "scan_error"} for issue in scan.issues):
            # A partial scan cannot prove ID uniqueness or absence. Retain the
            # previous projection rather than marking unseen pages missing.
            raise WikiScanUnavailableError(scan)
        signature = tuple(sorted(
            (str(page.page_id), page.vault_path, page.content_sha256)
            for page in scan.pages
        ))
        if signature != self._projection_signature:
            await self.repository.replace_projection(scan.pages)
            self._projection_signature = signature
        self.last_error = None
        return scan

    @staticmethod
    async def _file_call(function, *args):
        # Cancellation cannot stop a worker thread. Keep the service lock until
        # its file operation ends so a cancelled request cannot race another edit.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                if not task.done():
                    continue
                with suppress(Exception, asyncio.CancelledError):
                    task.result()
                raise
            else:
                if cancelled:
                    raise asyncio.CancelledError
                return result

    async def reconcile(self) -> WikiScan:
        async with self._lock:
            return await self._scan_locked()

    async def list_pages(self, *, limit: int = 100, offset: int = 0) -> dict:
        async with self._lock:
            scan = await self._scan_locked()
            pages = await self.repository.list_pages(limit=limit, offset=offset)
            return {
                "pages": pages,
                "issues": [
                    {"vault_path": issue.vault_path, "code": issue.code, "detail": issue.detail}
                    for issue in scan.issues
                ],
            }

    async def _detail_locked(self, page: WikiFile) -> dict:
        projected = await self.repository.get_page(page.page_id)
        if projected is None:
            raise WikiNotFoundError("Wiki page is not available")
        return {**projected, "markdown": page.markdown}

    @staticmethod
    def _find(scan: WikiScan, page_id: UUID) -> WikiFile:
        for page in scan.pages:
            if page.page_id == page_id:
                return page
        raise WikiNotFoundError("Wiki page is missing or invalid; inspect scan issues")

    async def get_page(self, page_id: UUID) -> dict:
        async with self._lock:
            scan = await self._scan_locked()
            return await self._detail_locked(self._find(scan, page_id))

    async def create(self, title: str, body: str) -> dict:
        async with self._lock:
            await self._scan_locked()
            page = await self._file_call(self.files.create, title, body)
            scan = await self._scan_locked()
            return await self._detail_locked(self._find(scan, page.page_id))

    async def save(self, page_id: UUID, markdown: str, expected_sha256: str) -> dict:
        async with self._lock:
            await self._scan_locked()
            try:
                await self._file_call(self.files.save, page_id, markdown, expected_sha256)
            except WikiConflictError:
                # Reconcile external changes before exposing current content.
                # The exception keeps the snapshot that caused the conflict.
                await self._scan_locked()
                raise
            scan = await self._scan_locked()
            return await self._detail_locked(self._find(scan, page_id))

    async def backlinks(self, page_id: UUID) -> dict:
        async with self._lock:
            scan = await self._scan_locked()
            self._find(scan, page_id)
            return {"pages": await self.repository.backlinks(page_id)}

    async def conflict_current(self, page: WikiFile | None) -> dict | None:
        if page is None:
            return None
        async with self._lock:
            projected = await self.repository.get_page(page.page_id)
            # Hash/path must describe the conflict snapshot, even if another
            # editor changed the file again while the response was assembled.
            same_snapshot = bool(
                projected
                and projected["content_sha256"] == page.content_sha256
                and projected["vault_path"] == page.vault_path
            )
            links = projected.get("links", []) if same_snapshot else [
                {
                    "target": link.target, "anchor": link.anchor, "label": link.label,
                    "embed": link.embed, "line": link.line, "to_page_id": None,
                }
                for link in page.links
            ]
            return {
                **(projected or {}),
                "page_id": str(page.page_id),
                "vault_path": page.vault_path,
                "title": page.title,
                "status": page.status,
                "content_sha256": page.content_sha256,
                "markdown": page.markdown,
                "updated_at": projected["updated_at"] if same_snapshot else datetime.now(UTC),
                "links": links,
            }

    def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._watch(), name="knowgrain-wiki-watch"),
            asyncio.create_task(self._periodic(), name="knowgrain-wiki-reconcile"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _background_scan(self) -> None:
        try:
            await self.reconcile()
        except Exception as exc:
            self.last_error = f"Wiki scan failed ({type(exc).__name__})"
            # Never log Markdown, names, file paths or raw DB exception details.
            logger.warning("%s; a later scan will retry", self.last_error)

    async def _watch(self) -> None:
        try:
            async for _changes in awatch(
                self.files.vault.root / "Wiki",
                stop_event=self._stop,
                debounce=400,
                step=100,
                watch_filter=lambda _kind, path: path.lower().endswith(".md"),
            ):
                await self._background_scan()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"Wiki watcher stopped ({type(exc).__name__})"
            logger.warning("%s; periodic scanning remains active", self.last_error)

    async def _periodic(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except TimeoutError:
                await self._background_scan()
