"""Byte-backed verification of retrieval chunks before M3 generation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from knowgrain.m3_types import (
    EligibleRevision,
    Evidence,
    EvidenceUnavailableError,
    evidence_identity,
)
from knowgrain.parsers import DocumentParseError, ParsedDocument, parse_document
from knowgrain.provenance_repository import ProvenanceRepository
from knowgrain.vault import VaultPathError, VaultStore


_MAX_CANDIDATE_CHUNKS = 50
_MAX_EVIDENCE_ITEMS = 24
_MAX_EXCERPT_CHARS = 6_000
_MAX_TOTAL_EVIDENCE_CHARS = 48_000
_MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
_MAX_CHUNK_CHARS = 1_000_000
_FILE_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _VerifiedOriginal:
    parsed: ParsedDocument


class ProvenanceService:
    """Collect and revalidate evidence against current Vault originals."""

    def __init__(self, repository: ProvenanceRepository, vault: VaultStore) -> None:
        self.repository = repository
        self.vault = vault

    async def collect(self, raw: dict) -> tuple[Evidence, ...]:
        """Filter retrieval output, then prove bounded quotes against current files."""
        chunks = self._candidate_chunks(raw)
        candidates: list[tuple[str, str, str, UUID | None]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            path = chunk.get("file_path")
            chunk_id = chunk.get("chunk_id")
            content = chunk.get("content")
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 2048
                or "\x00" in path
                or "\\" in path
                or not isinstance(chunk_id, str)
                or not chunk_id
                or len(chunk_id) > 256
                or any(ord(character) < 32 for character in chunk_id)
                or not isinstance(content, str)
                or not content
                or len(content) > _MAX_CHUNK_CHARS
            ):
                continue
            try:
                content.encode("utf-8")
                chunk_id.encode("utf-8")
            except UnicodeEncodeError:
                continue
            revision_id = None
            if "source_revision_id" in chunk:
                try:
                    revision_id = UUID(chunk["source_revision_id"])
                except (ValueError, TypeError, AttributeError):
                    continue
            candidates.append((path, chunk_id, content, revision_id))

        if not candidates:
            raise self._unavailable()

        eligible_by_path = await self.repository.eligible_by_paths(
            list(dict.fromkeys(path for path, _, _, identity in candidates if identity is None))
        )
        revision_ids = list(dict.fromkeys(
            identity for _, _, _, identity in candidates if identity is not None
        ))[:_MAX_EVIDENCE_ITEMS]
        eligible_by_id = await self.repository.eligible_by_ids(revision_ids) if revision_ids else {}
        loaded: dict[UUID, _VerifiedOriginal | None] = {}
        remaining_original_bytes = _MAX_ORIGINAL_BYTES
        evidence: list[Evidence] = []
        evidence_ids: set[UUID] = set()
        total_excerpt_chars = 0

        for path, chunk_id, chunk_text, revision_id in candidates:
            if len(evidence) >= _MAX_EVIDENCE_ITEMS:
                break
            eligible = (
                eligible_by_id.get(revision_id)
                if revision_id is not None else eligible_by_path.get(path)
            )
            if eligible is None:
                continue

            original = loaded.get(eligible.revision_id)
            if eligible.revision_id not in loaded:
                original, consumed = await asyncio.to_thread(
                    self._load_verified_original,
                    self.vault,
                    eligible,
                    remaining_original_bytes,
                )
                remaining_original_bytes -= consumed
                loaded[eligible.revision_id] = original
            if original is None:
                continue

            # Verify the complete upstream chunk before creating its bounded quote.
            start = original.parsed.text.find(chunk_text)
            if start < 0 or original.parsed.text.find(chunk_text, start + 1) >= 0:
                continue
            excerpt = chunk_text[:_MAX_EXCERPT_CHARS]
            if not excerpt or total_excerpt_chars + len(excerpt) > _MAX_TOTAL_EVIDENCE_CHARS:
                continue
            end = start + len(excerpt)
            excerpt_sha256 = self._sha256_text(excerpt)
            evidence_id = evidence_identity(
                eligible.revision_id, chunk_id, excerpt_sha256
            )
            if evidence_id in evidence_ids:
                continue

            page, heading = self._location_for_range(original.parsed, start, end)
            evidence.append(
                Evidence(
                    evidence_id=evidence_id,
                    source_id=eligible.source_id,
                    revision_id=eligible.revision_id,
                    filename=eligible.filename,
                    vault_path=eligible.vault_path,
                    source_sha256=eligible.sha256,
                    parsed_text_sha256=eligible.parsed_text_sha256,
                    chunk_id=chunk_id,
                    excerpt=excerpt,
                    excerpt_sha256=excerpt_sha256,
                    start=start,
                    end=end,
                    page=page,
                    heading=heading,
                    indexed_at=eligible.indexed_at,
                )
            )
            evidence_ids.add(evidence_id)
            total_excerpt_chars += len(excerpt)

        if not evidence:
            raise self._unavailable()
        return tuple(evidence)

    async def validate(self, evidence: Sequence[Evidence]) -> None:
        """Recheck DB eligibility and all content-derived evidence fields."""
        if (
            not isinstance(evidence, Sequence)
            or isinstance(evidence, (str, bytes))
            or not evidence
            or len(evidence) > _MAX_EVIDENCE_ITEMS
            or any(not isinstance(item, Evidence) for item in evidence)
        ):
            raise self._unavailable()
        evidence_ids = [item.evidence_id for item in evidence]
        revision_ids = [item.revision_id for item in evidence]
        if any(
            not isinstance(identity, UUID)
            for identity in (*evidence_ids, *revision_ids)
        ):
            raise self._unavailable()
        if len(set(evidence_ids)) != len(evidence_ids):
            raise self._unavailable()

        eligible_by_id = await self.repository.eligible_by_ids(
            [item.revision_id for item in evidence]
        )
        loaded: dict[UUID, _VerifiedOriginal | None] = {}
        remaining_original_bytes = _MAX_ORIGINAL_BYTES
        total_excerpt_chars = 0

        for item in evidence:
            eligible = eligible_by_id.get(item.revision_id)
            if eligible is None or not self._matches_eligible(item, eligible):
                raise self._unavailable()

            original = loaded.get(item.revision_id)
            if item.revision_id not in loaded:
                original, consumed = await asyncio.to_thread(
                    self._load_verified_original,
                    self.vault,
                    eligible,
                    remaining_original_bytes,
                )
                remaining_original_bytes -= consumed
                loaded[item.revision_id] = original
            if original is None:
                raise self._unavailable()

            if (
                not isinstance(item.chunk_id, str)
                or not item.chunk_id
                or len(item.chunk_id) > 256
                or any(ord(character) < 32 for character in item.chunk_id)
                or not isinstance(item.excerpt, str)
                or not item.excerpt
                or len(item.excerpt) > _MAX_EXCERPT_CHARS
                or not isinstance(item.start, int)
                or isinstance(item.start, bool)
                or not isinstance(item.end, int)
                or isinstance(item.end, bool)
                or item.start < 0
                or item.end <= item.start
                or item.end > len(original.parsed.text)
                or item.end - item.start != len(item.excerpt)
                or original.parsed.text[item.start : item.end] != item.excerpt
                or self._sha256_text(item.excerpt) != item.excerpt_sha256
                or evidence_identity(
                    item.revision_id, item.chunk_id, item.excerpt_sha256
                )
                != item.evidence_id
            ):
                raise self._unavailable()
            try:
                item.chunk_id.encode("utf-8")
                item.excerpt.encode("utf-8")
            except UnicodeEncodeError:
                raise self._unavailable() from None

            total_excerpt_chars += len(item.excerpt)
            if total_excerpt_chars > _MAX_TOTAL_EVIDENCE_CHARS:
                raise self._unavailable()
            page, heading = self._location_for_range(
                original.parsed, item.start, item.end
            )
            if item.page != page or item.heading != heading:
                raise self._unavailable()

    @staticmethod
    def _candidate_chunks(raw: Any) -> list[Any]:
        if not isinstance(raw, dict):
            return []
        data = raw.get("data")
        if not isinstance(data, dict):
            return []
        chunks = data.get("chunks")
        if not isinstance(chunks, list):
            return []
        return chunks[:_MAX_CANDIDATE_CHUNKS]

    @staticmethod
    def _load_verified_original(
        vault: VaultStore, revision: EligibleRevision, byte_budget: int
    ) -> tuple[_VerifiedOriginal | None, int]:
        """Read at most the remaining collection budget and parse in a worker thread."""
        descriptor: int | None = None
        consumed = 0
        try:
            source_path = vault.resolve(revision.vault_path)
            descriptor = os.open(
                source_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > byte_budget:
                return None, 0

            contents: list[bytes] = []
            remaining = metadata.st_size
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = None
                while remaining:
                    block = source.read(min(remaining, _FILE_READ_CHUNK_BYTES))
                    if not block:
                        return None, consumed
                    contents.append(block)
                    remaining -= len(block)
                    consumed += len(block)
                # A prefix with the recorded size is insufficient: concurrent
                # append/replacement must not turn changed originals into proof.
                if source.read(1):
                    return None, consumed
                final = os.fstat(source.fileno())
                current = os.stat(source_path, follow_symlinks=False)
                signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                           value.st_mtime_ns, value.st_ctime_ns)
                if signature(final) != signature(metadata) or signature(current) != signature(metadata):
                    return None, consumed
            content = b"".join(contents)
            source_sha256 = hashlib.sha256(content).hexdigest()
            if source_sha256 != revision.sha256:
                return None, consumed
            parsed = parse_document(revision.filename, content)
            parsed_text_sha256 = ProvenanceService._sha256_text(parsed.text)
            if parsed_text_sha256 != revision.parsed_text_sha256:
                return None, consumed
            return (
                _VerifiedOriginal(parsed=parsed),
                consumed,
            )
        except (OSError, ValueError, DocumentParseError, VaultPathError):
            return None, consumed
        except Exception:
            # Parser and filesystem failures never expose source material to callers.
            return None, consumed
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _location_for_range(
        parsed: ParsedDocument, start: int, end: int
    ) -> tuple[int | None, str | None]:
        """Return labels only when exactly one parsed segment contains the quote."""
        segments_with_ranges: list[tuple[int, int, int | None, str | None]] = []
        cursor = 0
        for segment in parsed.segments:
            if not segment.text:
                continue
            segment_start = parsed.text.find(segment.text, cursor)
            if segment_start < 0:
                continue
            segment_end = segment_start + len(segment.text)
            segments_with_ranges.append(
                (segment_start, segment_end, segment.page, segment.heading)
            )
            cursor = segment_end

        matches = [
            (page, heading)
            for segment_start, segment_end, page, heading in segments_with_ranges
            if segment_start <= start and end <= segment_end
        ]
        if len(matches) != 1:
            return None, None
        return matches[0]

    @staticmethod
    def _matches_eligible(item: Evidence, eligible: EligibleRevision) -> bool:
        return bool(
            item.source_id == eligible.source_id
            and item.revision_id == eligible.revision_id
            and item.filename == eligible.filename
            and item.vault_path == eligible.vault_path
            and item.source_sha256 == eligible.sha256
            and item.parsed_text_sha256 == eligible.parsed_text_sha256
            and item.indexed_at == eligible.indexed_at
        )

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _unavailable() -> EvidenceUnavailableError:
        return EvidenceUnavailableError(
            "No current, indexed and verifiable evidence is available."
        )
