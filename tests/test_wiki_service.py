"""File/API integration checks with an explicitly in-memory projection double.

Actual PostgreSQL transaction semantics are tested separately; these checks
exercise real files and HTTP status/error payloads without model dependencies.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from uuid import UUID

import httpx
from fastapi import FastAPI

from knowgrain.upload_limits import UploadBodyLimitMiddleware
from knowgrain.vault import VaultStore
from knowgrain.wiki_api import install_wiki_routes
from knowgrain.wiki_files import WikiIssue, WikiScan, parse_wiki
from knowgrain.wiki_service import WikiService, WikiScanUnavailableError


class ProjectionDouble:
    def __init__(self):
        self.pages = {}
        self.replacements = 0

    async def replace_projection(self, pages):
        self.replacements += 1
        self.pages = {
            page.page_id: {
                "page_id": str(page.page_id), "vault_path": page.vault_path,
                "title": page.title, "status": page.status,
                "content_sha256": page.content_sha256,
                "updated_at": datetime.now(UTC), "links": [],
            }
            for page in pages
        }

    async def list_pages(self, *, limit, offset):
        return list(self.pages.values())[offset:offset + limit]

    async def get_page(self, page_id):
        return self.pages.get(page_id)

    async def backlinks(self, page_id):
        return []


class WikiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = VaultStore(Path(self.temporary.name) / "vault")
        self.vault.initialize()
        self.service = WikiService(self.vault, ProjectionDouble())
        self.app = FastAPI()
        self.app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=1024)
        install_wiki_routes(self.app)
        # No model/Core object exists: manual Wiki must not depend on one.
        self.runtime = SimpleNamespace(
            database=SimpleNamespace(is_ready=True), vault_ready=True,
            _runtime_lock=asyncio.Lock(), wiki=self.service,
        )
        self.app.state.runtime = self.runtime
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.stop()

    async def create_page(self):
        response = await self.client.post("/api/v1/wiki/pages", json={"title": "Local Wiki", "body": "Initial body"})
        self.assertEqual(response.status_code, 201, response.text)
        page = response.json()
        self.assertEqual(response.headers["location"], f"/api/v1/wiki/pages/{page['page_id']}")
        return page

    async def test_external_edit_conflict_returns_current_and_preserves_file(self):
        page = await self.create_page()
        path = self.vault.resolve(page["vault_path"])
        external = page["markdown"].replace("Initial body", "Obsidian changed this body")
        path.write_text(external, encoding="utf-8")
        stale = page["markdown"].replace("Initial body", "Unsaved Web edit")
        response = await self.client.put(f"/api/v1/wiki/pages/{page['page_id']}", json={
            "markdown": stale, "expected_sha256": page["content_sha256"],
        })
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertIsInstance(detail["current"]["updated_at"], str)
        self.assertIsNotNone(datetime.fromisoformat(detail["current"]["updated_at"]).tzinfo)
        self.assertEqual(detail["current"]["markdown"], external)
        self.assertIn("Obsidian changed", detail["diff"])
        self.assertEqual(path.read_text(encoding="utf-8"), external)
        current = await self.client.get(f"/api/v1/wiki/pages/{page['page_id']}")
        self.assertEqual(current.json()["markdown"], external)

    async def test_move_retains_id_and_manual_edit_saves_current_hash(self):
        page = await self.create_page()
        old = self.vault.resolve(page["vault_path"])
        new = self.vault.resolve("Wiki/Drafts/Nested/Renamed.md")
        new.parent.mkdir()
        old.rename(new)
        response = await self.client.get(f"/api/v1/wiki/pages/{page['page_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["vault_path"], "Wiki/Drafts/Nested/Renamed.md")
        response = await self.client.put(f"/api/v1/wiki/pages/{page['page_id']}", json={
            "markdown": page["markdown"].replace("Initial body", "Saved from Web"),
            "expected_sha256": page["content_sha256"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Saved from Web", new.read_text(encoding="utf-8"))
        self.assertFalse(old.exists())

    async def test_write_body_limit_and_unavailable_vault(self):
        response = await self.client.put("/api/v1/wiki/pages/00000000-0000-0000-0000-000000000001",
            content=b"{}", headers={"content-length": str(13 * 1024 * 1024)})
        self.assertEqual(response.status_code, 413)
        self.runtime.vault_ready = False
        response = await self.client.post("/api/v1/wiki/pages", json={"title": "No write", "body": ""})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(list(self.vault.root.glob("Wiki/Drafts/*.md")), [])

    async def test_incomplete_scan_retains_projection_and_prevents_creation(self):
        page = await self.create_page()
        previous = dict(self.service.repository.pages)
        self.service.files.scan = lambda: WikiScan(
            pages=(), issues=(WikiIssue("Wiki", "scan_limit", "Too many candidate files"),),
        )
        with self.assertRaises(WikiScanUnavailableError):
            await self.service.create("No new file", "Must not be written")
        self.assertEqual(self.service.repository.pages, previous)
        self.assertEqual(len(list(self.vault.root.glob("Wiki/Drafts/*.md"))), 1)
        response = await self.client.get("/api/v1/wiki/pages")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "scan_incomplete")
        self.assertEqual(self.service.repository.pages[UUID(page["page_id"])], previous[UUID(page["page_id"])])

    async def test_unsafe_managed_root_retains_previous_projection(self):
        await self.create_page()
        previous = dict(self.service.repository.pages)
        managed = self.vault.root / "Wiki/Drafts"
        original = managed.with_name("DraftsOriginal")
        managed.rename(original)
        managed.symlink_to(original, target_is_directory=True)
        scan = self.service.files.scan()
        self.assertFalse(scan.complete)
        with self.assertRaises(WikiScanUnavailableError):
            await self.service.reconcile()
        self.assertEqual(self.service.repository.pages, previous)
        self.assertEqual(len(list(original.glob("*.md"))), 1)

    async def test_unchanged_scans_do_not_reallocate_projection_links(self):
        page = await self.create_page()
        replacements = self.service.repository.replacements
        await self.service.reconcile()
        await self.service.list_pages()
        await self.service.get_page(UUID(page["page_id"]))
        self.assertEqual(self.service.repository.replacements, replacements)
        path = self.vault.resolve(page["vault_path"])
        path.write_text(page["markdown"].replace("Initial body", "Externally changed"), encoding="utf-8")
        await self.service.reconcile()
        self.assertEqual(self.service.repository.replacements, replacements + 1)

    async def test_conflict_snapshot_does_not_borrow_newer_links(self):
        page = await self.create_page()
        captured = parse_wiki(page["markdown"].replace("Initial body", "[[Captured]]"), page["vault_path"])
        projected = self.service.repository.pages[captured.page_id]
        projected["links"] = [{"target": "Newer", "to_page_id": str(captured.page_id)}]
        detail = await self.service.conflict_current(captured)
        self.assertEqual(detail["markdown"], captured.markdown)
        self.assertEqual(detail["links"][0]["target"], "Captured")
        self.assertIsNone(detail["links"][0]["to_page_id"])

    async def test_cancelled_file_write_keeps_service_lock_until_thread_finishes(self):
        page = await self.create_page()
        started, release = Event(), Event()
        original_save = self.service.files.save

        def delayed_save(*args):
            started.set()
            if not release.wait(timeout=3):
                raise AssertionError("test must release the file worker")
            return original_save(*args)

        self.service.files.save = delayed_save
        editing = asyncio.create_task(self.service.save(
            UUID(page["page_id"]), page["markdown"].replace("Initial body", "Finished edit"),
            page["content_sha256"],
        ))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            editing.cancel()
            # Cancellation is delivered while the file worker is still blocked.
            await asyncio.sleep(0)
            self.assertTrue(self.service._lock.locked())
            editing.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.service._lock.locked())
            self.assertFalse(editing.done())
            listing = asyncio.create_task(self.service.list_pages())
            await asyncio.sleep(0)
            self.assertFalse(listing.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await editing
        await listing
        current = await self.service.get_page(UUID(page["page_id"]))
        self.assertIn("Finished edit", current["markdown"])
