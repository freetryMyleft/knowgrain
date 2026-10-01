"""Current-revision eligibility queries for M3 evidence provenance."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from knowgrain.database import ApplicationDatabase
from knowgrain.m3_types import EligibleRevision
from knowgrain.models import SourceDocument, SourceRevision


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATHS = 50
_MAX_IDS = 24


class ProvenanceRepository:
    """Find source revisions that are safe to use for new evidence."""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    async def eligible_by_paths(self, paths: Sequence[str]) -> dict[str, EligibleRevision]:
        """Resolve exact Vault paths, excluding historical and ambiguous records."""
        unique_paths = self._bounded_unique(paths, _MAX_PATHS, "paths")
        if not unique_paths:
            return {}

        path_counts = (
            select(
                SourceRevision.vault_path.label("vault_path"),
                func.count(SourceRevision.id).label("revision_count"),
            )
            .where(SourceRevision.vault_path.in_(unique_paths))
            .group_by(SourceRevision.vault_path)
            .subquery()
        )
        async with self.database.session_factory() as session:
            result = await session.execute(
                select(
                    SourceDocument.id,
                    SourceDocument.state,
                    SourceDocument.latest_revision_id,
                    SourceDocument.current_revision_id,
                    SourceRevision.id,
                    SourceRevision.filename,
                    SourceRevision.vault_path,
                    SourceRevision.sha256,
                    SourceRevision.parsed_text_sha256,
                    SourceRevision.index_state,
                    SourceRevision.indexed_at,
                )
                .join(SourceRevision, SourceRevision.source_id == SourceDocument.id)
                .join(
                    path_counts,
                    path_counts.c.vault_path == SourceRevision.vault_path,
                )
                .where(path_counts.c.revision_count == 1)
                .where(SourceRevision.vault_path.in_(unique_paths))
            )
            by_path: dict[str, list[tuple]] = defaultdict(list)
            for row in result.all():
                by_path[row[6]].append(row)

        # A Vault path must have one unambiguous revision record in the database,
        # including historical or otherwise ineligible records.
        eligible: dict[str, EligibleRevision] = {}
        for path in unique_paths:
            records = by_path.get(path, [])
            if len(records) != 1:
                continue
            row = records[0]
            if self._is_eligible_values(
                state=row[1],
                latest_revision_id=row[2],
                current_revision_id=row[3],
                revision_id=row[4],
                source_sha256=row[7],
                parsed_text_sha256=row[8],
                index_state=row[9],
                indexed_at=row[10],
            ):
                eligible[path] = EligibleRevision(
                    source_id=row[0],
                    revision_id=row[4],
                    filename=row[5],
                    vault_path=row[6],
                    sha256=row[7],
                    parsed_text_sha256=row[8],
                    indexed_at=row[10],
                )
        return eligible

    async def eligible_by_ids(self, ids: Sequence[UUID]) -> dict[UUID, EligibleRevision]:
        """Recheck the complete eligibility predicate for evidence identities."""
        unique_ids = self._bounded_unique(ids, _MAX_IDS, "ids")
        if not unique_ids:
            return {}

        async with self.database.session_factory() as session:
            result = await session.execute(
                select(
                    SourceDocument.id,
                    SourceDocument.state,
                    SourceDocument.latest_revision_id,
                    SourceDocument.current_revision_id,
                    SourceRevision.id,
                    SourceRevision.filename,
                    SourceRevision.vault_path,
                    SourceRevision.sha256,
                    SourceRevision.parsed_text_sha256,
                    SourceRevision.index_state,
                    SourceRevision.indexed_at,
                )
                .join(SourceRevision, SourceRevision.source_id == SourceDocument.id)
                .where(SourceRevision.id.in_(unique_ids))
            )
            rows = result.all()
            paths = list({row[6] for row in rows})
            path_counts = {}
            if paths:
                count_result = await session.execute(
                    select(SourceRevision.vault_path, func.count(SourceRevision.id))
                    .where(SourceRevision.vault_path.in_(paths))
                    .group_by(SourceRevision.vault_path)
                )
                path_counts = dict(count_result.all())

        eligible: dict[UUID, EligibleRevision] = {}
        for row in rows:
            if (
                path_counts.get(row[6]) == 1
                and self._is_eligible_values(
                    state=row[1],
                    latest_revision_id=row[2],
                    current_revision_id=row[3],
                    revision_id=row[4],
                    source_sha256=row[7],
                    parsed_text_sha256=row[8],
                    index_state=row[9],
                    indexed_at=row[10],
                )
            ):
                eligible[row[4]] = EligibleRevision(
                    source_id=row[0],
                    revision_id=row[4],
                    filename=row[5],
                    vault_path=row[6],
                    sha256=row[7],
                    parsed_text_sha256=row[8],
                    indexed_at=row[10],
                )
        return eligible

    @staticmethod
    def _bounded_unique(values: Sequence, maximum: int, name: str) -> list:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError(f"{name} must be a sequence")
        unique: list = []
        seen: set = set()
        for value in values:
            if name == "paths":
                valid = isinstance(value, str) and bool(value) and len(value) <= 2048
            else:
                valid = isinstance(value, UUID)
            if not valid:
                raise ValueError(f"{name} contains an invalid value")
            if value not in seen:
                seen.add(value)
                unique.append(value)
                if len(unique) > maximum:
                    raise ValueError(f"{name} exceeds the supported limit")
        return unique

    @staticmethod
    def _is_eligible_values(
        *,
        state: str,
        latest_revision_id: UUID | None,
        current_revision_id: UUID | None,
        revision_id: UUID,
        source_sha256: str,
        parsed_text_sha256: str | None,
        index_state: str,
        indexed_at: datetime | None,
    ) -> bool:
        return bool(
            state == "active"
            and current_revision_id == revision_id
            and latest_revision_id == revision_id
            and index_state == "ready"
            and isinstance(parsed_text_sha256, str)
            and _SHA256_PATTERN.fullmatch(parsed_text_sha256)
            and isinstance(source_sha256, str)
            and _SHA256_PATTERN.fullmatch(source_sha256)
            and indexed_at is not None
        )
