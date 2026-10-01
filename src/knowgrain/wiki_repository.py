from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Sequence
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from knowgrain.database import ApplicationDatabase
from knowgrain.models import PageLink, WikiPage

if TYPE_CHECKING:
    from knowgrain.wiki_files import WikiFile, WikiLink


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[a-zA-Z]:")
_PROJECTION_LOCK_KEYS = (1263420247, 1464428111)


class WikiRepository:
    """PostgreSQL projection for Wiki file identities and parsed links.

    Markdown bodies stay in the Vault. Every replacement is one serialized
    snapshot, so readers see either the previous complete projection or the new one.
    """

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    async def replace_projection(self, pages: Sequence[WikiFile]) -> None:
        page_rows = list(pages)
        ids = [page.page_id for page in page_rows]
        if len(ids) != len(set(ids)):
            raise ValueError("projection contains duplicate Wiki page IDs")
        for page in page_rows:
            if page.status not in {"draft", "reviewed"}:
                raise ValueError("Wiki page status must be draft or reviewed")
            if not _SHA256_PATTERN.fullmatch(page.content_sha256):
                raise ValueError("Wiki page content_sha256 must be lowercase SHA-256")
            if not isinstance(page.vault_path, str) or not page.vault_path:
                raise ValueError("Wiki page vault_path must not be empty")
            if not isinstance(page.title, str) or not page.title:
                raise ValueError("Wiki page title must not be empty")

        resolutions = self._resolve_targets(page_rows)
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_a, :lock_b)"),
                {"lock_a": _PROJECTION_LOCK_KEYS[0], "lock_b": _PROJECTION_LOCK_KEYS[1]},
            )
            await session.execute(
                update(WikiPage).values(present=False, updated_at=text("now()"))
            )
            # asyncpg accepts at most 32767 bound arguments per statement.
            # Keep all batches in this transaction and below that driver limit.
            for start in range(0, len(page_rows), 1000):
                statement = pg_insert(WikiPage).values(
                    [
                        {
                            "id": page.page_id,
                            "vault_path": page.vault_path,
                            "title": page.title,
                            "status": page.status,
                            "content_sha256": page.content_sha256,
                            "present": True,
                        }
                        for page in page_rows[start:start + 1000]
                    ]
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[WikiPage.id],
                        set_={
                            "vault_path": statement.excluded.vault_path,
                            "title": statement.excluded.title,
                            "status": statement.excluded.status,
                            "content_sha256": statement.excluded.content_sha256,
                            "present": True,
                            "updated_at": text("now()"),
                        },
                    )
                )

            await session.execute(delete(PageLink))
            session.add_all(
                [
                    PageLink(
                        from_page_id=page.page_id,
                        to_page_id=to_page_id,
                        target=link.target,
                        anchor=link.anchor,
                        label=link.label,
                        embed=link.embed,
                        line=link.line,
                    )
                    for page in page_rows
                    for link, to_page_id in resolutions[page.page_id]
                ]
            )

    async def list_pages(self, limit: int = 100, offset: int = 0) -> list[dict]:
        self._validate_pagination(limit, offset)
        async with self.database.session_factory() as session:
            rows = await session.scalars(
                select(WikiPage)
                .where(WikiPage.present.is_(True))
                .order_by(WikiPage.title, WikiPage.vault_path, WikiPage.id)
                .limit(limit)
                .offset(offset)
            )
            return [self._summary(page) for page in rows]

    async def get_page(self, page_id: UUID) -> dict | None:
        async with self.database.session_factory() as session:
            page = await session.scalar(
                select(WikiPage).where(WikiPage.id == page_id, WikiPage.present.is_(True))
            )
            if page is None:
                return None
            links = await session.scalars(
                select(PageLink).where(PageLink.from_page_id == page_id).order_by(PageLink.id)
            )
            return {
                **self._summary(page),
                "links": [self._link_snapshot(link) for link in links],
            }

    async def backlinks(self, page_id: UUID) -> list[dict]:
        target_page = aliased(WikiPage, name="target_page")
        async with self.database.session_factory() as session:
            rows = await session.execute(
                select(WikiPage, PageLink)
                .join(PageLink, PageLink.from_page_id == WikiPage.id)
                .join(target_page, target_page.id == PageLink.to_page_id)
                .where(
                    WikiPage.present.is_(True),
                    target_page.id == page_id,
                    target_page.present.is_(True),
                )
                .order_by(WikiPage.title, WikiPage.vault_path, WikiPage.id, PageLink.id)
            )
            return [
                {
                    **self._summary(page),
                    "target": link.target,
                    "anchor": link.anchor,
                    "line": link.line,
                    "embed": link.embed,
                }
                for page, link in rows
            ]

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be zero or greater")

    @staticmethod
    def _summary(page: WikiPage) -> dict:
        return {
            "page_id": str(page.id),
            "vault_path": page.vault_path,
            "title": page.title,
            "status": page.status,
            "content_sha256": page.content_sha256,
            "updated_at": page.updated_at,
        }

    @staticmethod
    def _link_snapshot(link: PageLink) -> dict:
        return {
            "target": link.target,
            "anchor": link.anchor,
            "label": link.label,
            "embed": link.embed,
            "line": link.line,
            "to_page_id": str(link.to_page_id) if link.to_page_id is not None else None,
        }

    @classmethod
    def _resolve_targets(
        cls, pages: Sequence[WikiFile]
    ) -> dict[UUID, list[tuple[WikiLink, UUID | None]]]:
        paths: dict[str, list[UUID]] = {}
        aliases: dict[str, set[UUID]] = {}
        for page in pages:
            normalized_path = cls._normalize_path(page.vault_path)
            if normalized_path is None:
                continue
            paths.setdefault(normalized_path, []).append(page.page_id)
            stem = PurePosixPath(page.vault_path).stem.casefold()
            title = page.title.casefold()
            aliases.setdefault(stem, set()).add(page.page_id)
            aliases.setdefault(title, set()).add(page.page_id)

        resolved: dict[UUID, list[tuple[WikiLink, UUID | None]]] = {}
        for page in pages:
            page_links: list[tuple[WikiLink, UUID | None]] = []
            for link in page.links:
                to_page_id = cls._resolve_one(
                    page, link.target, link.anchor, paths, aliases
                )
                page_links.append((link, to_page_id))
            resolved[page.page_id] = page_links
        return resolved

    @classmethod
    def _resolve_one(
        cls,
        source: WikiFile,
        target: str,
        anchor: str | None,
        paths: dict[str, list[UUID]],
        aliases: dict[str, set[UUID]],
    ) -> UUID | None:
        if not target:
            return source.page_id if anchor else None

        target_path = cls._normalize_target(target)
        if target_path is None:
            return None

        candidates = cls._path_candidates(target_path, source.vault_path)
        for candidate in candidates:
            matches = paths.get(candidate, [])
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None

        # An explicit path that is missing must not bind to an unrelated page
        # with the same basename (especially future Sources/Evidence targets).
        if "/" in target_path:
            return None
        # A bare target may refer to a unique filename stem or page title.
        bare = PurePosixPath(target_path).name
        matches = aliases.get(target_path.casefold(), set()) | aliases.get(bare.casefold(), set())
        if len(matches) == 1:
            return next(iter(matches))
        return None

    @classmethod
    def _path_candidates(cls, target: str, source_path: str) -> tuple[str, ...]:
        target_without_extension = cls._without_md(target)
        direct = cls._normalize_path(target_without_extension)
        source_relative = cls._normalize_path(
            posixpath.join(posixpath.dirname(source_path), target_without_extension)
        )
        return tuple(dict.fromkeys(path for path in (direct, source_relative) if path))

    @classmethod
    def _normalize_target(cls, target: str) -> str | None:
        # Link parsing preserves the target; this routine only builds safe lookup keys.
        target = target.replace("\\", "/")
        decoded = target
        for _ in range(3):
            decoded = unquote(decoded).replace("\\", "/")
            if decoded.startswith("/") or _WINDOWS_ABSOLUTE.match(decoded):
                return None
            if any(segment == ".." for segment in decoded.split("/")):
                return None
        if target.startswith("/") or _WINDOWS_ABSOLUTE.match(target):
            return None
        segments = target.split("/")
        if any(segment == ".." for segment in segments):
            return None
        normalized = posixpath.normpath(target)
        if normalized in {"", ".", ".."} or normalized.startswith("../"):
            return None
        return cls._without_md(normalized)

    @staticmethod
    def _without_md(path: str) -> str:
        return path[:-3] if path.casefold().endswith(".md") else path

    @classmethod
    def _normalize_path(cls, path: str) -> str | None:
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or _WINDOWS_ABSOLUTE.match(normalized):
            return None
        parts = normalized.split("/")
        if any(part in {"..", ""} for part in parts):
            return None
        normalized = cls._without_md(posixpath.normpath(normalized))
        if normalized in {"", ".", ".."} or normalized.startswith("../"):
            return None
        return normalized.casefold()
