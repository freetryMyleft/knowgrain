"""Durable, same-loop evidence-first Wiki generation coordination."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import difflib
from datetime import UTC, datetime
import json
import logging
import re
from uuid import UUID, uuid4, uuid5

import yaml

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
from knowgrain.review_files import ReviewFileStore
from knowgrain.review_repository import ReviewRepository
from knowgrain.wiki_files import (
    MAX_DIFF_BYTES,
    WikiConflictError,
    WikiFile,
    WikiNotFoundError,
    WikiScan,
    WikiValidationError,
    _frontmatter,
    parse_wiki,
)
from knowgrain.wiki_service import WikiScanUnavailableError, WikiService


logger = logging.getLogger(__name__)
_REVIEW_OPERATION_NAMESPACE = UUID("782f6808-20a4-5685-9256-8d693ee18935")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
        *,
        review_repository: ReviewRepository | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.provenance = provenance
        self.lightrag = lightrag
        self.wiki = wiki
        self.files = GenerationFileStore(wiki.files)
        self.review_files = ReviewFileStore(wiki.files)
        self.review_repository = review_repository or ReviewRepository(repository.database)
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
            binding = await self.review_repository.get_binding(page_id)
            generation_page_id = (
                UUID(binding["generation_page_id"]) if binding else page_id
            )
            manifest = await self.repository.get_generation(generation_page_id)
            if manifest is None:
                raise WikiNotFoundError("Page has no generation manifest")
            valid = True
            try:
                await self.provenance.validate(restore_evidence(manifest["evidence"]))
            except EvidenceUnavailableError:
                valid = False
            expected = (
                binding["reviewed_sha256"]
                if binding
                else manifest["reviewed_sha256"] or manifest["generated_sha256"]
            )
            is_generation_page = page_id == generation_page_id
            proposal = None
            proposal_target_id = manifest.get("proposal_target_page_id")
            proposal_target_sha = manifest.get("proposal_target_sha256")
            if is_generation_page and proposal_target_id and proposal_target_sha:
                try:
                    target = self.wiki._find(scan, UUID(proposal_target_id))
                except WikiNotFoundError:
                    target = None
                target_current_sha = target.content_sha256 if target else "0" * 64
                target_body = _frontmatter(target.markdown)[1] if target else ""
                proposal_body = _frontmatter(page.markdown)[1]
                proposal = {
                    "target_page_id": proposal_target_id,
                    "target_title": target.title[:200] if target else "目标页面已不存在",
                    "target_sha256": target_current_sha,
                    "expected_target_sha256": proposal_target_sha,
                    "target_changed": target is None or target_current_sha != proposal_target_sha,
                    "diff": self._bounded_diff(
                        target_body,
                        proposal_body,
                        fromfile="target",
                        tofile="proposal",
                    ),
                }
            return {
                **manifest,
                "page_id": str(page_id),
                "generation_page_id": str(generation_page_id),
                "proposal_target_page_id": proposal_target_id if is_generation_page else None,
                "proposal_target_sha256": proposal_target_sha if is_generation_page else None,
                "proposal": proposal,
                "reviewed_at": binding["reviewed_at"] if binding else manifest["reviewed_at"],
                "reviewed_sha256": expected if binding else manifest["reviewed_sha256"],
                "current_sha256": page.content_sha256,
                "content_modified": page.content_sha256 != expected,
                "evidence_current": valid,
                "status": page.status,
                "vault_path": page.vault_path,
            }

    async def review_page(self, page_id: UUID, expected_sha256: str) -> dict:
        """Explicitly transition one untouched generated draft into reviewed state."""
        self._validate_review_hash(expected_sha256, "expected_sha256")
        async with self.wiki._lock:
            scan, pages, duplicate_ids = await self._review_scan_locked(
                {page_id: {expected_sha256}}
            )
            page = pages.get(page_id)
            if page is None:
                raise WikiNotFoundError("Generated Wiki page does not exist")
            manifest = await self.repository.get_generation(page_id)
            if manifest is None:
                raise WikiNotFoundError("Page has no generation manifest")
            if manifest.get("proposal_target_page_id") is not None:
                raise WikiConflictError(
                    "Proposal drafts must be applied to their target page",
                    current=page, code="proposal_requires_apply", diff="",
                )
            if manifest["generated_sha256"] != expected_sha256:
                raise WikiConflictError(
                    "Generated draft changed; refresh before reviewing",
                    current=page, code="stale_generation", diff="",
                )
            if page.content_sha256 not in {expected_sha256} and page.status != "reviewed":
                raise WikiConflictError(
                    "Generated draft changed; refresh before reviewing",
                    current=page, code="stale_content", diff="",
                )

            metadata, body = _frontmatter(page.markdown)
            reviewed_markdown = self._render_reviewed_markdown(
                {**metadata, "kg_id": str(page_id), "kg_status": "reviewed"}, body
            )
            reviewed_page = parse_wiki(
                reviewed_markdown, f"Wiki/Pages/{page_id}.md"
            )
            operation_id = self._review_operation_id(
                "review", page_id, page_id, expected_sha256,
                reviewed_page.content_sha256,
            )
            await self._repair_review_projection(
                scan,
                pages,
                {page_id},
                duplicate_ids=duplicate_ids,
                retry_page_id=page_id,
                operation_id=operation_id,
                expected_sha256=expected_sha256,
                reviewed_markdown=reviewed_markdown,
            )
            evidence = restore_evidence(manifest["evidence"])
            await self.provenance.validate(evidence)
            await self.review_repository.prepare(
                operation_id,
                page_id=page_id,
                generation_page_id=page_id,
                expected_page_sha256=expected_sha256,
                expected_generation_sha256=manifest["generated_sha256"],
                reviewed_sha256=reviewed_page.content_sha256,
            )
            await self.provenance.validate(evidence)
            await self.wiki._file_call(
                self.review_files.commit,
                operation_id,
                page_id,
                expected_sha256,
                reviewed_markdown,
            )
            scan = await self.wiki._scan_locked()
            current = self.wiki._find(scan, page_id)
            if (
                current.content_sha256 != reviewed_page.content_sha256
                or current.status != "reviewed"
            ):
                raise WikiConflictError(
                    "Reviewed page changed before completion",
                    current=current, code="review_projection_changed", diff="",
                )
            await self.provenance.validate(evidence)
            await self.review_repository.complete(operation_id)
            return await self.wiki._detail_locked(current)

    async def apply_proposal(
        self,
        proposal_id: UUID,
        *,
        expected_proposal_sha256: str,
        expected_target_sha256: str,
    ) -> dict:
        """Explicitly apply one generated proposal to its unchanged target."""
        self._validate_review_hash(expected_proposal_sha256, "expected_proposal_sha256")
        self._validate_review_hash(expected_target_sha256, "expected_target_sha256")
        async with self.wiki._lock:
            manifest = await self.repository.get_generation(proposal_id)
            if manifest is None:
                raise WikiNotFoundError("Proposal has no generation manifest")
            raw_target_id = manifest.get("proposal_target_page_id")
            original_target_sha = manifest.get("proposal_target_sha256")
            if not raw_target_id or not original_target_sha:
                raise WikiConflictError(
                    "Generated page is not a proposal",
                    current=None, code="not_a_proposal", diff="",
                )
            target_id = UUID(raw_target_id)
            if expected_target_sha256 != original_target_sha:
                raise WikiConflictError(
                    "Proposal target version does not match the reviewed request",
                    current=None, code="stale_target", diff="",
                )
            scan, pages, duplicate_ids = await self._review_scan_locked(
                {
                    target_id: {expected_target_sha256},
                    proposal_id: {expected_proposal_sha256},
                }
            )
            target = pages.get(target_id)
            proposal_page = pages.get(proposal_id)
            if target is None or proposal_page is None:
                raise WikiNotFoundError("Proposal or target page does not exist")
            if manifest["generated_sha256"] != expected_proposal_sha256:
                raise WikiConflictError(
                    "Proposal draft changed; refresh before applying",
                    current=proposal_page, code="stale_proposal", diff="",
                )
            if (
                proposal_page.status != "draft"
                or proposal_page.content_sha256 != expected_proposal_sha256
            ):
                raise WikiConflictError(
                    "Proposal draft changed; refresh before applying",
                    current=proposal_page, code="stale_proposal", diff="",
                )

            target_metadata, _target_body = _frontmatter(target.markdown)
            proposal_metadata, proposal_body = _frontmatter(proposal_page.markdown)
            reviewed_metadata = {
                key: value for key, value in target_metadata.items()
                if not key.startswith("kg_")
            }
            reviewed_metadata.update(
                {
                    key: value for key, value in proposal_metadata.items()
                    if key.startswith("kg_")
                    and key not in {"kg_proposal_target", "kg_proposal_target_sha256"}
                }
            )
            reviewed_metadata.update(
                {"kg_id": str(target_id), "kg_kind": "wiki", "kg_status": "reviewed"}
            )
            reviewed_markdown = self._render_reviewed_markdown(
                reviewed_metadata, proposal_body
            )
            reviewed_page = parse_wiki(
                reviewed_markdown, f"Wiki/Pages/{target_id}.md"
            )
            # Fresh requests may use only the exact proposed target snapshot;
            # a prepared retry may also observe its exact reviewed output.
            if (
                target.content_sha256
                not in {expected_target_sha256, reviewed_page.content_sha256}
                or (
                    target.content_sha256 == reviewed_page.content_sha256
                    and target.status != "reviewed"
                )
            ):
                raise WikiConflictError(
                    "Proposal target changed; refresh before applying",
                    current=target, code="stale_target", diff="",
                )
            operation_id = self._review_operation_id(
                "apply", target_id, proposal_id, expected_target_sha256,
                reviewed_page.content_sha256,
            )
            await self._repair_review_projection(
                scan,
                pages,
                {target_id, proposal_id},
                duplicate_ids=duplicate_ids,
                retry_page_id=target_id,
                operation_id=operation_id,
                expected_sha256=expected_target_sha256,
                reviewed_markdown=reviewed_markdown,
            )
            evidence = restore_evidence(manifest["evidence"])
            await self.provenance.validate(evidence)
            await self.review_repository.prepare(
                operation_id,
                page_id=target_id,
                generation_page_id=proposal_id,
                expected_page_sha256=expected_target_sha256,
                expected_generation_sha256=expected_proposal_sha256,
                reviewed_sha256=reviewed_page.content_sha256,
            )
            await self.provenance.validate(evidence)
            await self.wiki._file_call(
                self.review_files.commit,
                operation_id,
                target_id,
                expected_target_sha256,
                reviewed_markdown,
            )
            scan = await self.wiki._scan_locked()
            current = self.wiki._find(scan, target_id)
            if (
                current.content_sha256 != reviewed_page.content_sha256
                or current.status != "reviewed"
            ):
                raise WikiConflictError(
                    "Reviewed proposal target changed before completion",
                    current=current, code="review_projection_changed", diff="",
                )
            await self.provenance.validate(evidence)
            await self.review_repository.complete(operation_id)
            return await self.wiki._detail_locked(current)

    async def _review_scan_locked(
        self, expected_hashes: dict[UUID, set[str]]
    ) -> tuple[WikiScan, dict[UUID, WikiFile], set[UUID]]:
        """Read candidates without changing the database projection."""
        scan = await self.wiki._file_call(self.wiki.files.scan)
        if (
            not scan.complete
            or any(issue.code in {"scan_limit", "scan_error"} for issue in scan.issues)
        ):
            raise WikiScanUnavailableError(scan)
        if any(issue.code == "unsafe_path" for issue in scan.issues):
            raise WikiConflictError(
                "An unsafe Wiki path prevents review recovery",
                current=None, code="unsafe_path", diff="",
            )

        pages: dict[UUID, WikiFile] = {page.page_id: page for page in scan.pages}
        duplicate_ids: set[UUID] = set()
        for issue in scan.issues:
            if issue.code != "duplicate_id":
                continue
            match = re.match(r"page id ([0-9a-fA-F-]{36}) is also used by:", issue.detail)
            if match:
                duplicate_ids.add(UUID(match.group(1)))
        for page_id, hashes in expected_hashes.items():
            if page_id in duplicate_ids:
                candidates: list[WikiFile] = []
            else:
                candidates = [page for page in scan.pages if page.page_id == page_id]
            duplicates = [
                issue.vault_path
                for issue in scan.issues
                if issue.code == "duplicate_id"
                and f"page id {page_id} " in issue.detail
            ]
            for path in sorted(set(duplicates)):
                try:
                    raw = await self.wiki._file_call(self.wiki.files._read_regular, path)
                    markdown = raw.decode("utf-8", errors="strict")
                    candidates.append(parse_wiki(markdown, path))
                except (OSError, UnicodeDecodeError, WikiValidationError):
                    continue
            if not candidates:
                raise WikiNotFoundError("Wiki page is missing or invalid")
            exact = [candidate for candidate in candidates if candidate.content_sha256 in hashes]
            if len(exact) > 1:
                raise WikiConflictError(
                    "Wiki page identity is ambiguous during review recovery",
                    current=None, code="duplicate_id", diff="",
                )
            if exact:
                selected = exact[0]
            else:
                reviewed = [candidate for candidate in candidates if candidate.status == "reviewed"]
                if len(candidates) > 1 and len(reviewed) != 1:
                    raise WikiConflictError(
                        "Wiki page identity is ambiguous during review recovery",
                        current=None, code="duplicate_id", diff="",
                    )
                selected = reviewed[0] if reviewed else candidates[0]
            pages[page_id] = selected

        return scan, pages, duplicate_ids

    async def _repair_review_projection(
        self,
        scan: WikiScan,
        pages: dict[UUID, WikiFile],
        expected_page_ids: set[UUID],
        *,
        duplicate_ids: set[UUID],
        retry_page_id: UUID,
        operation_id: UUID,
        expected_sha256: str,
        reviewed_markdown: str,
    ) -> None:
        """Repair only an exact journaled partial move before prepare()."""
        if duplicate_ids:
            if duplicate_ids != {retry_page_id}:
                raise WikiConflictError(
                    "Wiki page identities are duplicated or ambiguous",
                    current=None, code="duplicate_id", diff="",
                )
            original = pages.get(retry_page_id)
            if original is None or original.content_sha256 != expected_sha256:
                raise WikiConflictError(
                    "Partial review does not match the requested original page",
                    current=original, code="duplicate_id", diff="",
                )
            journaled = await self.wiki._file_call(
                self.review_files.validate_retry_intent,
                operation_id,
                retry_page_id,
                expected_sha256,
                reviewed_markdown,
            )
            if not journaled:
                raise WikiConflictError(
                    "Duplicate Wiki identity has no matching review operation",
                    current=None, code="duplicate_id", diff="",
                )

        projected_by_id = {page.page_id: page for page in scan.pages}
        for page_id in expected_page_ids:
            selected = pages.get(page_id)
            if selected is None:
                raise WikiNotFoundError("Wiki page is missing or invalid")
            projected_by_id[page_id] = selected
        projected = sorted(
            projected_by_id.values(),
            key=lambda page: (page.vault_path.casefold(), page.vault_path, str(page.page_id)),
        )
        await self.wiki.repository.replace_projection(projected)
        self.wiki._projection_signature = tuple(sorted(
            (str(page.page_id), page.vault_path, page.content_sha256)
            for page in projected
        ))

    @staticmethod
    def _render_reviewed_markdown(metadata: dict, body: str) -> str:
        normalized = dict(metadata)
        if isinstance(normalized.get("kg_id"), UUID):
            normalized["kg_id"] = str(normalized["kg_id"])
        try:
            frontmatter = yaml.safe_dump(
                normalized, allow_unicode=True, sort_keys=False,
                default_flow_style=False, width=1000,
            )
        except (yaml.YAMLError, TypeError, ValueError) as exc:
            raise WikiValidationError("Wiki frontmatter cannot be rendered safely") from exc
        return f"---\n{frontmatter}---\n{body}"

    @staticmethod
    def _review_operation_id(
        kind: str,
        page_id: UUID,
        generation_page_id: UUID,
        old_sha256: str,
        new_sha256: str,
    ) -> UUID:
        key = ":".join(
            (kind, str(page_id), str(generation_page_id), old_sha256, new_sha256)
        )
        return uuid5(_REVIEW_OPERATION_NAMESPACE, key)

    @staticmethod
    def _validate_review_hash(value: str, name: str) -> None:
        if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
            raise WikiValidationError(f"{name} must be a lowercase SHA-256 digest")

    @staticmethod
    def _bounded_diff(
        before: str, after: str, *, fromfile: str, tofile: str
    ) -> str:
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=fromfile, tofile=tofile,
            )
        )
        encoded = diff.encode("utf-8")
        if len(encoded) <= MAX_DIFF_BYTES:
            return diff
        suffix = b"\n[diff truncated]\n"
        prefix = encoded[: MAX_DIFF_BYTES - len(suffix)].decode("utf-8", errors="ignore")
        return prefix + suffix.decode("ascii")

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
