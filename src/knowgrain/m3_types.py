"""Shared, provider-independent provenance contracts for Wiki generation."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from uuid import NAMESPACE_URL, UUID, uuid5


EVIDENCE_NAMESPACE = uuid5(NAMESPACE_URL, "urn:knowgrain:evidence:v1")


def evidence_identity(revision_id: UUID, chunk_id: str, excerpt_sha256: str) -> UUID:
    name = json.dumps([str(revision_id), chunk_id, excerpt_sha256], separators=(",", ":"))
    return uuid5(EVIDENCE_NAMESPACE, name)


@dataclass(frozen=True, slots=True)
class EligibleRevision:
    source_id: UUID
    revision_id: UUID
    filename: str
    vault_path: str
    sha256: str
    parsed_text_sha256: str
    indexed_at: datetime


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: UUID
    source_id: UUID
    revision_id: UUID
    filename: str
    vault_path: str
    source_sha256: str
    parsed_text_sha256: str
    chunk_id: str
    excerpt: str
    excerpt_sha256: str
    start: int
    end: int
    page: int | None
    heading: str | None
    indexed_at: datetime

    def snapshot(self) -> dict:
        value = asdict(self)
        for key in ("evidence_id", "source_id", "revision_id"):
            value[key] = str(value[key])
        value["indexed_at"] = self.indexed_at.isoformat()
        return value


class EvidenceUnavailableError(ValueError):
    """No current, indexed and byte-verifiable supporting evidence exists."""
