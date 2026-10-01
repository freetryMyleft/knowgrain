"""Projection transaction tests; require an explicitly selected disposable DB."""

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4

from sqlalchemy import delete

from knowgrain.config import Settings
from knowgrain.database import ApplicationDatabase, VaultBindingConflict
from knowgrain.models import PageLink, WikiPage
from knowgrain.wiki_repository import WikiRepository


TEST_DATABASE = os.environ.get("KNOWGRAIN_TEST_DATABASE", "")


@dataclass(frozen=True)
class FixtureLink:
    target: str
    anchor: str | None = None
    label: str | None = None
    embed: bool = False
    line: int = 1


@dataclass(frozen=True)
class FixtureWikiFile:
    page_id: UUID
    vault_path: str
    title: str
    status: str
    content_sha256: str
    markdown: str = ""
    links: tuple[FixtureLink, ...] = ()


def page(
    path: str,
    title: str | None = None,
    *,
    page_id: UUID | None = None,
    links=(),
) -> FixtureWikiFile:
    return FixtureWikiFile(
        page_id=page_id or uuid4(),
        vault_path=path,
        title=title or Path(path).stem,
        status="draft",
        content_sha256=hashlib.sha256((path + (title or "")).encode()).hexdigest(),
        links=tuple(links),
    )


@unittest.skipUnless(TEST_DATABASE == "knowgrain_test", "disposable knowgrain_test DB not selected")
class PostgresWikiRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            _env_file=None,
            postgres_host="127.0.0.1",
            postgres_port=int(os.environ.get("KNOWGRAIN_TEST_POSTGRES_PORT", "55432")),
            postgres_user="knowgrain",
            postgres_password="knowgrain-local",
            postgres_database=TEST_DATABASE,
            knowgrain_postgres_db=TEST_DATABASE,
            vault_root=Path(self.temporary.name) / "vault",
            vault_parent_dir=Path(self.temporary.name) / "vaults",
        )
        self.database = ApplicationDatabase(self.settings)
        self.assertTrue(await self.database.initialize(), self.database.last_error)
        binding, managed_content_count = await self.database.get_vault_state()
        if binding is not None or managed_content_count:
            await self.database.close()
            self.skipTest("fixture database must start without a Vault binding or managed content")
        self.repository = WikiRepository(self.database)
        self.page_ids: set[UUID] = set()

    async def asyncTearDown(self):
        if not getattr(self, "database", None) or not self.database.is_ready:
            return
        if self.page_ids:
            async with self.database.session_factory() as session, session.begin():
                ids = list(self.page_ids)
                await session.execute(
                    delete(PageLink).where(
                        (PageLink.from_page_id.in_(ids)) | (PageLink.to_page_id.in_(ids))
                    )
                )
                await session.execute(delete(WikiPage).where(WikiPage.id.in_(ids)))
        await self.database.close()

    async def project(self, *pages: FixtureWikiFile):
        self.page_ids.update(page.page_id for page in pages)
        await self.repository.replace_projection(pages)

    async def test_large_snapshot_is_batched_below_asyncpg_bind_limit(self):
        # 5500 minimal pages previously produced >32767 bound parameters.
        pages = [page(f"Wiki/Drafts/batch-{number}.md") for number in range(5500)]
        await self.project(*pages)
        self.assertEqual((await self.database.get_vault_state())[1], 5500)
        self.assertEqual(len(await self.repository.list_pages(limit=500)), 500)

    async def test_concurrent_scans_publish_one_complete_snapshot(self):
        left = [page(f"Wiki/Pages/left-{i}.md") for i in range(3)]
        right = [page(f"Wiki/Pages/right-{i}.md") for i in range(4)]
        self.page_ids.update(p.page_id for p in (*left, *right))

        await asyncio.gather(
            self.repository.replace_projection(left),
            self.repository.replace_projection(right),
        )

        listed = await self.repository.list_pages(limit=20)
        actual = {UUID(snapshot["page_id"]) for snapshot in listed}
        self.assertIn(actual, ({p.page_id for p in left}, {p.page_id for p in right}))
        for absent in ({p.page_id for p in right}, {p.page_id for p in left}):
            if actual != absent:
                for page_id in absent:
                    self.assertIsNone(await self.repository.get_page(page_id))

    async def test_rename_and_two_page_swap_keep_stable_ids(self):
        first_id, second_id = uuid4(), uuid4()
        first = page("Wiki/Pages/First.md", page_id=first_id)
        second = page("Wiki/Pages/Second.md", page_id=second_id)
        await self.project(first, second)

        renamed_first = page("Wiki/Pages/Renamed.md", page_id=first_id)
        swapped_second = page("Wiki/Pages/First.md", page_id=second_id)
        await self.project(renamed_first, swapped_second)

        first_snapshot = await self.repository.get_page(first_id)
        second_snapshot = await self.repository.get_page(second_id)
        self.assertEqual(first_snapshot["vault_path"], "Wiki/Pages/Renamed.md")
        self.assertEqual(second_snapshot["vault_path"], "Wiki/Pages/First.md")
        self.assertEqual(
            {item["page_id"] for item in await self.repository.list_pages()},
            {str(first_id), str(second_id)},
        )

    async def test_links_resolve_paths_and_reject_ambiguous_or_traversing_targets(self):
        source_id = uuid4()
        target_id = uuid4()
        local_id = uuid4()
        ambiguous_one = page("Wiki/Pages/Duplicate.md", "Duplicate", page_id=uuid4())
        ambiguous_two = page("Wiki/Drafts/Duplicate.md", "Duplicate", page_id=uuid4())
        target = page("Wiki/Pages/Target.md", page_id=target_id)
        local = page("Wiki/Drafts/Folder/Local.md", page_id=local_id)
        source = page(
            "Wiki/Drafts/Folder/Source.md",
            page_id=source_id,
            links=(
                FixtureLink("Wiki/Pages/Target", "part", "Target", line=4),
                FixtureLink("", "local-heading", line=6),
                FixtureLink("Duplicate", line=8),
                FixtureLink("..\\Pages\\Target", line=10),
                FixtureLink("wiki/pages/target.md", line=12, embed=True),
                FixtureLink("%2e%2e/Pages/Target", line=14),
                FixtureLink("Local", line=16),
            ),
        )
        await self.project(source, target, local, ambiguous_one, ambiguous_two)

        detail = await self.repository.get_page(source_id)
        links = detail["links"]
        self.assertEqual(links[0]["to_page_id"], str(target_id))
        self.assertEqual(links[0]["anchor"], "part")
        self.assertEqual(links[1]["to_page_id"], str(source_id))
        self.assertIsNone(links[2]["to_page_id"])
        self.assertIsNone(links[3]["to_page_id"])
        self.assertEqual(links[3]["target"], "..\\Pages\\Target")
        self.assertEqual(links[4]["to_page_id"], str(target_id))
        self.assertIsNone(links[5]["to_page_id"])
        self.assertEqual(links[6]["to_page_id"], str(local_id))
        backlinks = await self.repository.backlinks(target_id)
        self.assertEqual(len(backlinks), 2)
        self.assertEqual({entry["page_id"] for entry in backlinks}, {str(source_id)})
        self.assertEqual({entry["line"] for entry in backlinks}, {4, 12})

    async def test_absent_pages_are_tombstoned_and_removed_from_backlinks(self):
        target_id, source_id = uuid4(), uuid4()
        target = page("Wiki/Pages/Target.md", page_id=target_id)
        source = page(
            "Wiki/Pages/Source.md",
            page_id=source_id,
            links=(FixtureLink("Target", line=3),),
        )
        await self.project(target, source)
        self.assertEqual(len(await self.repository.backlinks(target_id)), 1)

        await self.project(target)

        self.assertIsNone(await self.repository.get_page(source_id))
        self.assertEqual(await self.repository.backlinks(target_id), [])
        async with self.database.session_factory() as session:
            tombstone = await session.get(WikiPage, source_id)
        self.assertIsNotNone(tombstone)
        self.assertFalse(tombstone.present)
        self.assertEqual(await self.database.get_vault_state(), (None, 2))

    async def test_wiki_only_identity_counts_toward_vault_binding_lock(self):
        wiki_page = page("Wiki/Drafts/Only-page.md")
        await self.project(wiki_page)

        binding, managed_content_count = await self.database.get_vault_state()
        self.assertIsNone(binding)
        self.assertEqual(managed_content_count, 1)
        with self.assertRaises(VaultBindingConflict):
            await self.database.compare_and_set_vault_binding(
                expected_binding_id=None,
                expected_root="/vault/old",
                target_root="/vault/new",
            )
