"""Bounded candidate lookup for revision-backed Wiki/entity navigation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select, union

from knowgrain.database import ApplicationDatabase
from knowgrain.models import (
    EvidenceRef,
    GeneratedPage,
    PageEvidence,
    PageGenerationBinding,
    WikiPage,
)


_MAX_CHUNK_IDS = 10_000
_MAX_PAGE_LIMIT = 50
_MAX_CHUNK_ID_LENGTH = 512


class EntityMappingRepository:
    """Find only pages whose selected generation manifest cites candidate chunks."""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    async def candidate_page_ids(
        self, chunk_ids: Sequence[str], limit: int = 50
    ) -> dict[str, Any]:
        chunks = self._normalize_chunk_ids(chunk_ids)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_LIMIT
        ):
            raise ValueError("limit must be between 1 and 50")
        if not chunks:
            return {"page_ids": (), "truncated": False}

        # A reviewed target resolves through its current binding to the selected
        # generation manifest. Its own older GeneratedPage is deliberately not
        # considered while a binding exists.
        bound_candidates = (
            select(PageGenerationBinding.page_id.label("page_id"))
            .join(
                GeneratedPage,
                GeneratedPage.page_id == PageGenerationBinding.generation_page_id,
            )
            .join(WikiPage, WikiPage.id == PageGenerationBinding.page_id)
            .join(PageEvidence, PageEvidence.page_id == GeneratedPage.page_id)
            .join(EvidenceRef, EvidenceRef.evidence_id == PageEvidence.evidence_id)
            .where(
                WikiPage.present.is_(True),
                EvidenceRef.chunk_id.in_(chunks),
            )
        )

        # A page with no reviewed binding may use only its own generation. A
        # separate proposal manifest cannot be mistaken for another page's data.
        has_binding = exists(
            select(PageGenerationBinding.page_id).where(
                PageGenerationBinding.page_id == GeneratedPage.page_id
            )
        )
        own_candidates = (
            select(GeneratedPage.page_id.label("page_id"))
            .join(WikiPage, WikiPage.id == GeneratedPage.page_id)
            .join(PageEvidence, PageEvidence.page_id == GeneratedPage.page_id)
            .join(EvidenceRef, EvidenceRef.evidence_id == PageEvidence.evidence_id)
            .where(
                WikiPage.present.is_(True),
                EvidenceRef.chunk_id.in_(chunks),
                ~has_binding,
            )
        )
        candidate_ids = union(bound_candidates, own_candidates).subquery()

        async with self.database.session_factory() as session:
            rows = (
                await session.scalars(
                    select(WikiPage.id)
                    .join(candidate_ids, candidate_ids.c.page_id == WikiPage.id)
                    .where(WikiPage.present.is_(True))
                    .order_by(WikiPage.title, WikiPage.vault_path, WikiPage.id)
                    .limit(limit + 1)
                )
            ).all()
        truncated = len(rows) > limit
        return {"page_ids": tuple(rows[:limit]), "truncated": truncated}

    @staticmethod
    def _normalize_chunk_ids(chunk_ids: Sequence[str]) -> tuple[str, ...]:
        if isinstance(chunk_ids, (str, bytes)) or not isinstance(chunk_ids, Sequence):
            raise ValueError("chunk_ids must be a sequence")
        if len(chunk_ids) > _MAX_CHUNK_IDS:
            raise ValueError("chunk_ids exceeds the safe candidate bound")
        normalized: set[str] = set()
        for chunk_id in chunk_ids:
            if (
                not isinstance(chunk_id, str)
                or not chunk_id
                or len(chunk_id) > _MAX_CHUNK_ID_LENGTH
                or "\x00" in chunk_id
            ):
                raise ValueError("chunk_ids contains an invalid identifier")
            try:
                chunk_id.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise ValueError("chunk_ids contains invalid Unicode") from None
            normalized.add(chunk_id)
        return tuple(sorted(normalized))
