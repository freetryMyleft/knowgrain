"""Durable, same-loop evidence-first Wiki generation coordination."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import json
import logging
import re
from uuid import UUID, uuid4

from knowgrain.config import Settings
from knowgrain.generation_contract import (
    DraftValidationError,
    build_generation_prompt,
    parse_draft,
    render_draft,
    render_evidence,
)
from knowgrain.generation_files import GenerationFileStore
from knowgrain.generation_repository import GenerationRepository
from knowgrain.lightrag_runtime import LightRAGRuntime
from knowgrain.m3_types import Evidence, EvidenceUnavailableError
from knowgrain.provenance import ProvenanceService
from knowgrain.wiki_files import WikiConflictError, WikiNotFoundError, WikiValidationError
from knowgrain.wiki_service import WikiService


logger = logging.getLogger(__name__)


class GenerationLeaseLostError(RuntimeError):
    pass


def restore_evidence(values: list[dict]) -> tuple[Evidence, ...]:
    """Restore retained server snapshots; never trust browser-supplied evidence."""
    items = []
    try:
        for value in values:
            fields = dict(value)
            for key in ("evidence_id", "source_id", "revision_id"):
                fields[key] = UUID(fields[key])
            fields["indexed_at"] = datetime.fromisoformat(fields["indexed_at"])
            items.append(Evidence(**fields))
    except (TypeError, ValueError, KeyError) as exc:
        raise EvidenceUnavailableError("Retained evidence manifest is invalid") from exc
    return tuple(items)


class GenerationService:
    def __init__(
        self,
        settings: Settings,
        repository: GenerationRepository,
        provenance: ProvenanceService,
        lightrag: LightRAGRuntime,
        wiki: WikiService,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.provenance = provenance
        self.lightrag = lightrag
        self.wiki = wiki
        self.files = GenerationFileStore(wiki.files)
        self.owner = uuid4()
        self._task: asyncio.Task | None = None

    async def enqueue(
        self, topic: str, *, target_page_id: UUID | None = None,
        expected_target_sha256: str | None = None,
    ) -> dict:
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 600 or "\0" in topic:
            raise WikiValidationError("Generation topic must contain 1–600 characters")
        if (target_page_id is None) != (expected_target_sha256 is None):
            raise WikiValidationError("Proposal requires both target page and content hash")
        if target_page_id is not None:
            current = await self.wiki.get_page(target_page_id)
            if not re.fullmatch(r"[0-9a-f]{64}", expected_target_sha256 or ""):
                raise WikiValidationError("Proposal target hash is invalid")
            if current["content_sha256"] != expected_target_sha256:
                raise WikiConflictError(
                    "Proposal target changed; refresh the page first",
                    current=None, code="stale_target", diff="",
                )
        return await self.repository.enqueue(
            topic.strip(), target_page_id=target_page_id,
            expected_target_sha256=expected_target_sha256,
        )

    async def generation_detail(self, page_id: UUID) -> dict:
        """Inspect retained claims with current file/evidence validity for review."""
        async with self.wiki._lock:
            scan = await self.wiki._scan_locked()
            page = self.wiki._find(scan, page_id)
            manifest = await self.repository.get_generation(page_id)
            if manifest is None:
                raise WikiNotFoundError("Page has no generation manifest")
            valid = True
            try:
                await self.provenance.validate(restore_evidence(manifest["evidence"]))
            except EvidenceUnavailableError:
                valid = False
            expected = manifest["reviewed_sha256"] or manifest["generated_sha256"]
            return {
                **manifest,
                "current_sha256": page.content_sha256,
                "content_modified": page.content_sha256 != expected,
                "evidence_current": valid,
                "status": page.status,
                "vault_path": page.vault_path,
            }

    async def evidence_detail(self, evidence_id: UUID) -> dict:
        """Return a retained quote; stale evidence stays visible but is labelled."""
        evidence = await self.repository.get_evidence(evidence_id)
        if evidence is None:
            raise WikiNotFoundError("Evidence does not exist")
        valid = True
        try:
            await self.provenance.validate((evidence,))
        except EvidenceUnavailableError:
            valid = False
        return {**evidence.snapshot(), "current": valid,
                "evidence_path": f"Sources/Evidence/{evidence.evidence_id}.md"}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="knowgrain-generation-jobs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.repository.database.is_ready:
            try:
                await self.repository.release_owner(self.owner)
            except Exception as exc:
                logger.warning("Generation lease release failed (%s)", type(exc).__name__)

    async def _run(self) -> None:
        while True:
            if (not self.repository.database.is_ready or not self.lightrag.is_ready
                    or self.lightrag.restart_required or self.lightrag.model_validation_error is not None):
                await asyncio.sleep(1)
                continue
            try:
                job = await self.repository.claim(self.owner)
                if job is not None:
                    await self._execute(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Generation executor unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(1)

    async def _execute(self, job: dict) -> None:
        job_id = UUID(str(job["job_id"]))
        processing = asyncio.create_task(self._process(job), name=f"generate-{job_id}")
        lease = asyncio.create_task(self._renew(job_id), name=f"generation-lease-{job_id}")
        try:
            done, _ = await asyncio.wait({processing, lease}, return_when=asyncio.FIRST_COMPLETED)
            if lease in done:
                await lease
                raise GenerationLeaseLostError("Generation lease expired")
            await processing
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, EvidenceUnavailableError):
                detail = "资料中未找到当前有效、已索引且可核实的依据；请检查资料修订或重新索引"
            elif isinstance(exc, DraftValidationError):
                detail = "模型草稿不符合声明与引用规则；未自动发布，请重试"
            elif isinstance(exc, WikiConflictError):
                detail = "目标草稿或证据文件已变化；保留人工内容，请检查冲突"
            elif isinstance(exc, TimeoutError):
                detail = "模型检索或生成超时；原件未被修改，可重试"
            else:
                detail = f"生成失败 ({type(exc).__name__})；检查服务后重试"
            try:
                await self.repository.fail(job_id, self.owner, detail)
            except Exception as record_error:
                logger.warning("Generation failure recording failed (%s)", type(record_error).__name__)
        finally:
            processing.cancel()
            lease.cancel()
            # File publication drains through WikiService._file_call even when
            # cancelled, before the Wiki service or Core can be shut down.
            await asyncio.gather(processing, lease, return_exceptions=True)

    async def _renew(self, job_id: UUID) -> None:
        while True:
            await asyncio.sleep(20)
            if not await self.repository.renew(job_id, self.owner):
                raise GenerationLeaseLostError("Generation lease was lost")

    async def _process(self, job: dict) -> None:
        job_id, page_id = UUID(str(job["job_id"])), UUID(str(job["output_page_id"]))
        result = job.get("result")
        if result is None:
            async with asyncio.timeout(240):
                raw = await self.lightrag.retrieve(job["topic"], mode="mix")
            evidence = await self.provenance.collect(raw)
            listed = await self.wiki.list_pages(limit=20)
            # Keep exact paths for link rendering while bounding retained model
            # metadata (8 KiB). Large valid Wiki catalogs must not break jobs.
            related = []
            for page in listed["pages"]:
                candidate = {key: page[key] for key in ("page_id", "title", "vault_path")}
                candidate["title"] = candidate["title"][:200]
                proposed = [*related, candidate]
                if len(json.dumps(proposed, ensure_ascii=False).encode("utf-8")) <= 4096:
                    related = proposed
                if len(related) >= 8:
                    break
            system, prompt = build_generation_prompt(job["topic"], evidence, related)
            response = await self.lightrag.generate_json(system, prompt)
            draft = parse_draft(response, evidence, related)
            cited = {str(item) for section in draft.sections for claim in section.claims for item in claim.evidence_ids}
            evidence = tuple(item for item in evidence if str(item.evidence_id) in cited)
            await self.provenance.validate(evidence)
            model = {
                "name": self.settings.llm_model, "provider": "ollama",
                "generated_at": datetime.now(UTC).isoformat(), "related_pages": related,
            }
            draft_data = draft.model_dump(mode="json")
            if not await self.repository.store_result(
                job_id, self.owner, draft=draft_data, evidence=evidence, model=model,
            ):
                raise GenerationLeaseLostError("Generation lease was lost before publication")
            result = {"draft": draft_data, "evidence": [item.snapshot() for item in evidence], "model": model}
        evidence = restore_evidence(result["evidence"])
        model = result["model"]
        related = model.get("related_pages", [])
        draft = parse_draft(json.dumps(result["draft"], ensure_ascii=False), evidence, related)
        markdown = render_draft(
            draft, evidence, related, page_id=page_id, job_id=job_id,
            model=model["name"], generated_at=model["generated_at"],
            target_page_id=UUID(str(job["target_page_id"])) if job.get("target_page_id") else None,
            target_sha256=job.get("expected_target_sha256"),
        )
        evidence_pages = [(item.evidence_id, render_evidence(item)) for item in evidence]
        async with self.wiki._lock:
            await self.wiki._scan_locked()
            await self.provenance.validate(evidence)
            published = await self.wiki._file_call(self.files.publish, page_id, markdown, evidence_pages)
            scan = await self.wiki._scan_locked()
            current = self.wiki._find(scan, page_id)
            if current.content_sha256 != published.content_sha256:
                raise WikiConflictError("Generated file changed before completion", current=current, code="generation_modified", diff="")
            claims = [
                {"key": claim.key, "evidence_ids": [str(item) for item in claim.evidence_ids]}
                for section in draft.sections for claim in section.claims
            ]
            if not await self.repository.complete(
                job_id, self.owner, page_id=page_id,
                content_sha256=current.content_sha256, claims=claims,
            ):
                raise GenerationLeaseLostError("Generation lease was lost during completion")
