"""Read-only navigation between current Wiki pages and source-backed entities."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from knowgrain.entity_mapping_repository import EntityMappingRepository
from knowgrain.generation_service import GenerationService, restore_evidence
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.m3_types import Evidence, EvidenceUnavailableError
from knowgrain.wiki_service import WikiService
from knowgrain.wiki_files import WikiNotFoundError


_MAX_ENTITY_NAME_LENGTH = 512
_MAX_ENTITY_TYPE_LENGTH = 256
_MAX_ENTITY_RESULTS = 100
_MAX_CANDIDATE_PAGES = 50
_MAX_ENTITY_CHUNKS = 10_000


class EntityMappingNotFoundError(LookupError):
    """The requested Wiki page does not exist in the current Vault scan."""


class EntityMappingValidationError(ValueError):
    """A requested entity name is not a valid exact lookup key."""


class EntityMappingUnavailableError(RuntimeError):
    """Core, storage, or a complete current Vault scan was unavailable."""


class EntityMappingService:
    def __init__(
        self,
        generation: GenerationService,
        repository: EntityMappingRepository,
        lightrag: LightRAGRuntime,
        wiki: WikiService,
    ) -> None:
        self.generation = generation
        self.repository = repository
        self.lightrag = lightrag
        self.wiki = wiki

    async def page_entities(self, page_id: UUID | str) -> dict[str, Any]:
        try:
            identity = self._page_id(page_id)
            page = await self.wiki.get_page(identity)
            binding = await self.generation.review_repository.get_binding(identity)
            generation_page_id = (
                UUID(binding["generation_page_id"]) if binding else identity
            )
            manifest = await self.generation.repository.get_generation(generation_page_id)
            if manifest is None:
                # A binding without its immutable manifest is damaged projection
                # state, while an unbound page without a manifest is manual.
                return self._empty_page_result(
                    identity,
                    page["content_sha256"],
                    binding_current=False,
                    evidence_current=False,
                )

            initial = await self.generation.generation_detail(identity)
            context = self._valid_context(initial)
            if context is None:
                return self._empty_page_result(
                    identity,
                    initial.get("current_sha256", page["content_sha256"]),
                    binding_current=not bool(initial.get("content_modified")),
                    evidence_current=bool(initial.get("evidence_current")),
                )
            evidence = self._restore_detail_evidence(initial)
            if evidence is None:
                return self._empty_page_result(
                    identity,
                    initial["current_sha256"],
                    binding_current=context["binding_current"],
                    evidence_current=False,
                )

            raw = await self.lightrag.entities_for_evidence(evidence)
            entities, truncated = self._parse_entities(raw, evidence)
            latest = await self.generation.generation_detail(identity)
            latest_context = self._valid_context(latest)
            current_page = await self.wiki.get_page(identity)
            page_hash_matches = (
                latest_context is not None
                and current_page["content_sha256"] == latest_context["current_sha256"]
            )
            if (
                latest_context is None
                or not self._same_context(context, latest_context)
                or not page_hash_matches
            ):
                return self._empty_page_result(
                    identity,
                    current_page["content_sha256"],
                    binding_current=(
                        latest_context["binding_current"] if latest_context and page_hash_matches else False
                    ),
                    evidence_current=(
                        latest_context["evidence_current"] if latest_context else False
                    ),
                )
            return {
                "page_id": str(identity),
                "content_sha256": latest["current_sha256"],
                "binding_current": latest_context["binding_current"],
                "evidence_current": latest_context["evidence_current"],
                "entities": entities,
                "truncated": truncated,
            }
        except (EntityMappingNotFoundError, EntityMappingValidationError):
            raise
        except WikiNotFoundError:
            raise EntityMappingNotFoundError("Wiki page does not exist") from None
        except Exception as exc:
            raise EntityMappingUnavailableError("Entity mapping is unavailable") from exc

    async def entity_pages(self, name: str) -> dict[str, Any]:
        try:
            name = self.validate_name(name)
            entity_id = self.entity_id(name)
            chunk_ids = self._core_chunk_ids(await self.lightrag.entity_chunk_ids(name))
            if not chunk_ids:
                return {
                    "entity_id": entity_id,
                    "name": name,
                    "pages": [],
                    "truncated": False,
                }

            candidates = await self.repository.candidate_page_ids(
                chunk_ids, limit=_MAX_CANDIDATE_PAGES
            )
            page_ids, truncated = self._candidate_result(candidates)
            core_truncated = False
            pages = []
            candidate_chunk_set = set(chunk_ids)
            for page_id in page_ids:
                try:
                    initial = await self.generation.generation_detail(page_id)
                except WikiNotFoundError:
                    # A scan can remove a candidate after the database selected it.
                    continue
                context = self._valid_context(initial)
                if context is None:
                    continue
                evidence = self._restore_detail_evidence(initial)
                if evidence is None:
                    continue
                candidate_chunks = tuple(
                    item for item in evidence if item.chunk_id in candidate_chunk_set
                )
                if not candidate_chunks:
                    continue

                raw = await self.lightrag.entities_for_evidence(candidate_chunks)
                entities, _ = self._parse_entities(raw, candidate_chunks)
                core_truncated = core_truncated or raw["truncated"] or len(raw["entities"]) > _MAX_ENTITY_RESULTS
                match = next((item for item in entities if item["name"] == name), None)
                if match is None:
                    continue

                try:
                    latest = await self.generation.generation_detail(page_id)
                    current_page = await self.wiki.get_page(page_id)
                except WikiNotFoundError:
                    continue
                latest_context = self._valid_context(latest)
                if (
                    latest_context is None
                    or not self._same_context(context, latest_context)
                    or current_page["content_sha256"] != latest_context["current_sha256"]
                ):
                    continue
                evidence_ids = {str(item.evidence_id) for item in candidate_chunks}
                citations = [
                    evidence_id
                    for evidence_id in match["evidence_ids"]
                    if evidence_id in evidence_ids
                ]
                if not citations:
                    continue
                pages.append(
                    {
                        "page_id": str(page_id),
                        "title": current_page["title"],
                        "vault_path": current_page["vault_path"],
                        "content_sha256": latest["current_sha256"],
                        "evidence_ids": citations,
                    }
                )
            return {
                "entity_id": entity_id,
                "name": name,
                "pages": pages,
                "truncated": truncated or core_truncated,
            }
        except (EntityMappingValidationError, EntityMappingUnavailableError):
            raise
        except Exception as exc:
            raise EntityMappingUnavailableError("Entity mapping is unavailable") from exc

    @classmethod
    def validate_name(cls, name: str) -> str:
        if not isinstance(name, str) or not 1 <= len(name) <= _MAX_ENTITY_NAME_LENGTH:
            raise EntityMappingValidationError("Entity name must contain 1–512 characters")
        if not name.strip() or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in name
        ):
            raise EntityMappingValidationError("Entity name is invalid")
        try:
            name.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise EntityMappingValidationError("Entity name contains invalid Unicode") from None
        return name

    @classmethod
    def _parse_entities(
        cls, raw: Any, evidence: Sequence[Evidence]
    ) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(raw, dict) or not isinstance(raw.get("entities"), list):
            raise EntityMappingUnavailableError("Core returned an invalid entity mapping")
        raw_truncated = raw.get("truncated")
        if not isinstance(raw_truncated, bool):
            raise EntityMappingUnavailableError("Core returned invalid truncation metadata")
        evidence_by_id = {str(item.evidence_id): item for item in evidence}
        entities = raw["entities"]
        truncated = raw_truncated or len(entities) > _MAX_ENTITY_RESULTS
        selected: dict[str, dict[str, Any]] = {}
        conflicted: set[str] = set()
        for value in entities[:_MAX_ENTITY_RESULTS]:
            if not isinstance(value, dict):
                continue
            try:
                name = cls.validate_name(value.get("name"))
                entity_type = value.get("entity_type")
                if (
                    not isinstance(entity_type, str)
                    or not entity_type.strip()
                    or len(entity_type) > _MAX_ENTITY_TYPE_LENGTH
                    or any(
                        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                        for char in entity_type
                    )
                ):
                    continue
                canonical_id = cls.entity_id(name)
                if value.get("entity_id", canonical_id) != canonical_id:
                    continue
                raw_evidence_ids = value.get("evidence_ids")
                if (
                    isinstance(raw_evidence_ids, (str, bytes))
                    or not isinstance(raw_evidence_ids, Sequence)
                    or not 1 <= len(raw_evidence_ids) <= 24
                ):
                    continue
                citation_ids = [str(UUID(str(item))) for item in raw_evidence_ids]
                if (
                    len(citation_ids) != len(set(citation_ids))
                    or not set(citation_ids) <= evidence_by_id.keys()
                ):
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            if canonical_id in conflicted:
                continue
            existing = selected.get(canonical_id)
            if existing is not None:
                if existing["entity_type"] != entity_type:
                    selected.pop(canonical_id, None)
                    conflicted.add(canonical_id)
                else:
                    existing["evidence_ids"] = sorted(set(existing["evidence_ids"]) | set(citation_ids))
                continue
            selected[canonical_id] = {
                "entity_id": canonical_id,
                "name": name,
                "entity_type": entity_type.strip(),
                "evidence_ids": sorted(citation_ids),
            }
        result = list(selected.values())
        result.sort(key=lambda item: (item["name"], item["entity_id"]))
        return result[:_MAX_ENTITY_RESULTS], truncated

    @staticmethod
    def entity_id(name: str) -> str:
        return hashlib.sha256(name.encode("utf-8", errors="strict")).hexdigest()

    @staticmethod
    def _page_id(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            raise EntityMappingNotFoundError("Wiki page does not exist") from None

    @staticmethod
    def _valid_context(detail: dict) -> dict[str, Any] | None:
        page_id = detail.get("page_id")
        generation_page_id = detail.get("generation_page_id")
        current_sha256 = detail.get("current_sha256")
        if (
            not isinstance(page_id, str)
            or not isinstance(generation_page_id, str)
            or not isinstance(current_sha256, str)
            or detail.get("content_modified") is not False
            or detail.get("evidence_current") is not True
        ):
            return None
        try:
            UUID(page_id)
            UUID(generation_page_id)
        except ValueError:
            return None
        if len(current_sha256) != 64 or any(char not in "0123456789abcdef" for char in current_sha256):
            return None
        return {
            "page_id": page_id,
            "generation_page_id": generation_page_id,
            "current_sha256": current_sha256,
            "binding_current": True,
            "evidence_current": True,
        }

    @staticmethod
    def _same_context(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return (
            left["page_id"] == right["page_id"]
            and left["generation_page_id"] == right["generation_page_id"]
            and left["current_sha256"] == right["current_sha256"]
            and right["binding_current"]
            and right["evidence_current"]
        )

    @staticmethod
    def _restore_detail_evidence(detail: dict) -> tuple[Evidence, ...] | None:
        try:
            value = detail.get("evidence")
            if not isinstance(value, list) or not value:
                return None
            return restore_evidence(value)
        except EvidenceUnavailableError:
            return None

    @staticmethod
    def _empty_page_result(
        page_id: UUID,
        content_sha256: str,
        *,
        binding_current: bool,
        evidence_current: bool,
    ) -> dict[str, Any]:
        return {
            "page_id": str(page_id),
            "content_sha256": content_sha256,
            "binding_current": binding_current,
            "evidence_current": evidence_current,
            "entities": [],
            "truncated": False,
        }

    @staticmethod
    def _core_chunk_ids(value: Any) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise EntityMappingUnavailableError("Core returned invalid candidate chunks")
        if len(value) > _MAX_ENTITY_CHUNKS:
            raise EntityMappingUnavailableError("Core candidate set exceeds its bound")
        unique: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 512 or "\x00" in item:
                raise EntityMappingUnavailableError("Core returned invalid candidate chunks")
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise EntityMappingUnavailableError("Core returned invalid candidate chunks") from None
            unique.add(item)
        return tuple(sorted(unique))

    @staticmethod
    def _candidate_result(value: Any) -> tuple[tuple[UUID, ...], bool]:
        if not isinstance(value, dict) or not isinstance(value.get("page_ids"), Sequence):
            raise EntityMappingUnavailableError("Candidate lookup returned an invalid result")
        if isinstance(value["page_ids"], (str, bytes)):
            raise EntityMappingUnavailableError("Candidate lookup returned an invalid result")
        truncated = value.get("truncated")
        if not isinstance(truncated, bool) or len(value["page_ids"]) > _MAX_CANDIDATE_PAGES:
            raise EntityMappingUnavailableError("Candidate lookup exceeded its result bound")
        page_ids: list[UUID] = []
        seen: set[UUID] = set()
        for item in value["page_ids"]:
            try:
                identity = item if isinstance(item, UUID) else UUID(str(item))
            except (ValueError, TypeError, AttributeError):
                raise EntityMappingUnavailableError("Candidate lookup returned an invalid page ID") from None
            if identity in seen:
                raise EntityMappingUnavailableError("Candidate lookup returned duplicate page IDs")
            seen.add(identity)
            page_ids.append(identity)
        return tuple(page_ids), truncated
