"""Transactional persistence for explicit generated-page reviews."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowgrain.database import ApplicationDatabase
from knowgrain.generation_repository import GenerationConflictError, GenerationRepository
from knowgrain.m3_types import EvidenceUnavailableError
from knowgrain.models import (
    GeneratedPage,
    GenerationJob,
    PageGenerationBinding,
    ReviewOperation,
    WikiPage,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReviewRepository:
    """Persist review intent and the current reviewed-page provenance binding."""

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database
        self._generation_repository = GenerationRepository(database)

    async def prepare(
        self,
        operation_id: UUID,
        *,
        page_id: UUID,
        generation_page_id: UUID,
        expected_page_sha256: str,
        expected_generation_sha256: str,
        reviewed_sha256: str,
    ) -> dict[str, Any]:
        """Create immutable review intent after revalidating the page and its evidence."""
        self._validate_ids(operation_id, page_id, generation_page_id)
        self._validate_hashes(
            expected_page_sha256=expected_page_sha256,
            expected_generation_sha256=expected_generation_sha256,
            reviewed_sha256=reviewed_sha256,
        )

        fields = {
            "page_id": page_id,
            "generation_page_id": generation_page_id,
            "expected_page_sha256": expected_page_sha256,
            "expected_generation_sha256": expected_generation_sha256,
            "reviewed_sha256": reviewed_sha256,
        }
        try:
            async with self.database.session_factory() as session, session.begin():
                operation = await session.scalar(
                    select(ReviewOperation)
                    .where(ReviewOperation.operation_id == operation_id)
                    .with_for_update()
                )
                pages = await self._lock_pages(session, page_id, generation_page_id)
                if operation is None:
                    # A concurrent first prepare may have committed while this request
                    # waited for the page locks. Read its ledger row without taking a
                    # reverse-order lock (complete() locks operation before pages).
                    operation = await session.scalar(
                        select(ReviewOperation).where(
                            ReviewOperation.operation_id == operation_id
                        )
                    )
                target = pages.get(page_id)
                generation_page = pages.get(generation_page_id)
                generation = await self._lock_generation(session, generation_page_id)
                if target is None or generation_page is None or generation is None:
                    raise GenerationConflictError("Review page or generation manifest is missing")

                if operation is not None and not self._operation_matches(operation, fields):
                    raise GenerationConflictError(
                        "Review operation ID was already used for another request"
                    )

                job = await self._validate_manifest(
                    session,
                    generation,
                    expected_generation_sha256=expected_generation_sha256,
                )
                await self._assert_current_evidence(session, job)
                self._validate_review_projection(
                    target=target,
                    generation_page=generation_page,
                    generation=generation,
                    operation=operation,
                    expected_page_sha256=expected_page_sha256,
                    reviewed_sha256=reviewed_sha256,
                )

                if operation is not None and operation.state == "completed":
                    binding = await self._lock_binding(session, page_id)
                    self._require_completed_binding(operation, binding, generation)
                elif operation is None:
                    now = await session.scalar(select(func.clock_timestamp()))
                    operation = ReviewOperation(
                        operation_id=operation_id,
                        **fields,
                        state="prepared",
                        created_at=now,
                    )
                    session.add(operation)
                    await session.flush()
                return self._operation_snapshot(operation)
        except IntegrityError as exc:
            raise GenerationConflictError(
                "Review operation conflicts with an existing record"
            ) from exc

    async def complete(self, operation_id: UUID) -> dict[str, Any]:
        """Publish the reviewed target binding and mark its intent complete atomically."""
        if not isinstance(operation_id, UUID):
            raise ValueError("operation_id must be a UUID")
        try:
            async with self.database.session_factory() as session, session.begin():
                operation = await session.scalar(
                    select(ReviewOperation)
                    .where(ReviewOperation.operation_id == operation_id)
                    .with_for_update()
                )
                if operation is None:
                    raise GenerationConflictError("Review operation does not exist")

                pages = await self._lock_pages(
                    session, operation.page_id, operation.generation_page_id
                )
                target = pages.get(operation.page_id)
                generation_page = pages.get(operation.generation_page_id)
                generation = await self._lock_generation(session, operation.generation_page_id)
                if target is None or generation_page is None or generation is None:
                    raise GenerationConflictError("Review page or generation manifest is missing")

                job = await self._validate_manifest(
                    session,
                    generation,
                    expected_generation_sha256=operation.expected_generation_sha256,
                )
                await self._assert_current_evidence(session, job)
                self._validate_review_projection(
                    target=target,
                    generation_page=generation_page,
                    generation=generation,
                    operation=operation,
                    expected_page_sha256=operation.expected_page_sha256,
                    reviewed_sha256=operation.reviewed_sha256,
                )
                if (
                    not target.present
                    or target.status != "reviewed"
                    or target.content_sha256 != operation.reviewed_sha256
                ):
                    raise GenerationConflictError(
                        "Reviewed Wiki projection does not match the operation"
                    )
                if operation.page_id != operation.generation_page_id and (
                    not generation_page.present
                    or generation_page.status != "draft"
                    or generation_page.content_sha256 != generation.generated_sha256
                ):
                    raise GenerationConflictError("Proposal draft changed before review completion")

                binding = await self._lock_binding(session, operation.page_id)
                if operation.state == "completed":
                    self._require_completed_binding(operation, binding, generation)
                    return self._operation_snapshot(operation)

                if operation.state != "prepared":
                    raise GenerationConflictError("Review operation is in an invalid state")

                if operation.page_id == operation.generation_page_id:
                    if (
                        generation.reviewed_sha256 is not None
                        and generation.reviewed_sha256 != operation.reviewed_sha256
                    ):
                        raise GenerationConflictError(
                            "Generated page was already reviewed with another hash"
                        )

                if binding is not None and binding.operation_id != operation.operation_id:
                    previous_operation = await session.get(ReviewOperation, binding.operation_id)
                    if (
                        previous_operation is None
                        or operation.created_at <= previous_operation.created_at
                    ):
                        raise GenerationConflictError(
                            "A newer review binding already exists for this page"
                        )
                    binding.generation_page_id = operation.generation_page_id
                    binding.reviewed_sha256 = operation.reviewed_sha256
                    binding.operation_id = operation.operation_id
                elif binding is None:
                    binding = PageGenerationBinding(
                        page_id=operation.page_id,
                        generation_page_id=operation.generation_page_id,
                        reviewed_sha256=operation.reviewed_sha256,
                        reviewed_at=await session.scalar(select(func.clock_timestamp())),
                        operation_id=operation.operation_id,
                    )
                    session.add(binding)
                elif (
                    binding.generation_page_id != operation.generation_page_id
                    or binding.reviewed_sha256 != operation.reviewed_sha256
                ):
                    raise GenerationConflictError(
                        "Review operation conflicts with the current binding"
                    )

                now = await session.scalar(select(func.clock_timestamp()))
                if operation.page_id == operation.generation_page_id:
                    if generation.reviewed_at is None:
                        generation.reviewed_at = now
                        generation.reviewed_sha256 = operation.reviewed_sha256
                    elif generation.reviewed_sha256 != operation.reviewed_sha256:
                        raise GenerationConflictError(
                            "Generated page review projection has changed"
                        )
                    if binding is not None:
                        binding.reviewed_at = generation.reviewed_at
                elif binding is not None:
                    binding.reviewed_at = now

                operation.state = "completed"
                operation.completed_at = now
                await session.flush()
                return self._operation_snapshot(operation)
        except IntegrityError as exc:
            raise GenerationConflictError(
                "Review completion conflicts with an existing record"
            ) from exc

    async def get_binding(self, page_id: UUID) -> dict[str, Any] | None:
        if not isinstance(page_id, UUID):
            raise ValueError("page_id must be a UUID")
        async with self.database.session_factory() as session:
            binding = await session.get(PageGenerationBinding, page_id)
            return None if binding is None else self._binding_snapshot(binding)

    async def _assert_current_evidence(self, session: AsyncSession, job: GenerationJob) -> None:
        result = job.result
        if (
            not isinstance(result, dict)
            or set(result) != {"draft", "evidence", "model"}
            or not isinstance(result.get("evidence"), list)
        ):
            raise EvidenceUnavailableError("Retained generation evidence is unavailable")
        if result.get("draft") is None:
            raise GenerationConflictError("Retained generation draft is unavailable")
        evidence = self._generation_repository._evidence_from_snapshots(result["evidence"])
        await self._generation_repository._assert_current_evidence(
            session, evidence, require_rows=True
        )

    async def _validate_manifest(
        self,
        session: AsyncSession,
        generation: GeneratedPage,
        *,
        expected_generation_sha256: str,
    ) -> GenerationJob:
        if generation.generated_sha256 != expected_generation_sha256:
            raise GenerationConflictError(
                "Generated manifest hash changed; review the current draft"
            )
        job = await session.get(GenerationJob, generation.generation_job_id)
        if (
            job is None
            or job.state != "succeeded"
            or job.output_page_id != generation.page_id
            or job.output_sha256 != generation.generated_sha256
            or not isinstance(job.result, dict)
            or set(job.result) != {"draft", "evidence", "model"}
            or job.result.get("draft") != generation.draft
        ):
            raise GenerationConflictError("Retained generation manifest is incomplete or changed")
        if generation.proposal_target_page_id is None:
            if job.target_page_id is not None or job.target_sha256 is not None:
                raise GenerationConflictError("Generation proposal metadata does not match its job")
        elif (
            generation.proposal_target_page_id != job.target_page_id
            or generation.proposal_target_sha256 != job.target_sha256
        ):
            raise GenerationConflictError("Generation proposal metadata does not match its job")
        return job

    @staticmethod
    def _validate_review_projection(
        *,
        target: WikiPage,
        generation_page: WikiPage,
        generation: GeneratedPage,
        operation: ReviewOperation | None,
        expected_page_sha256: str,
        reviewed_sha256: str,
    ) -> None:
        is_proposal = generation.page_id != target.id
        if not target.present:
            raise GenerationConflictError("Review target is missing")

        if is_proposal:
            if (
                generation.proposal_target_page_id != target.id
                or generation.proposal_target_sha256 != expected_page_sha256
                or not generation_page.present
                or generation_page.status != "draft"
                or generation_page.content_sha256 != generation.generated_sha256
            ):
                raise GenerationConflictError("Proposal no longer matches its generated manifest")
            valid_original = (
                target.content_sha256 == expected_page_sha256
                and target.status in {"draft", "reviewed"}
            )
            valid_reviewed = (
                target.content_sha256 == reviewed_sha256 and target.status == "reviewed"
            )
        else:
            if generation.proposal_target_page_id is not None:
                raise GenerationConflictError(
                    "A proposal draft cannot be reviewed as its target page"
                )
            if generation.generated_sha256 != expected_page_sha256:
                raise GenerationConflictError("Generated page no longer matches the review request")
            valid_original = (
                target.content_sha256 == expected_page_sha256 and target.status == "draft"
            )
            valid_reviewed = (
                target.content_sha256 == reviewed_sha256 and target.status == "reviewed"
            )

        if operation is None:
            if not valid_original:
                raise GenerationConflictError("Review target changed; refresh before reviewing")
            return
        if operation.state == "completed":
            if not valid_reviewed:
                raise GenerationConflictError(
                    "Completed review no longer matches the Wiki projection"
                )
        elif operation.state == "prepared":
            if not (valid_original or valid_reviewed):
                raise GenerationConflictError(
                    "Prepared review target changed; refresh before retrying"
                )
        else:
            raise GenerationConflictError("Review operation is in an invalid state")

    async def _lock_pages(
        self, session: AsyncSession, page_id: UUID, generation_page_id: UUID
    ) -> dict[UUID, WikiPage]:
        page_ids = sorted({page_id, generation_page_id}, key=lambda value: value.int)
        rows = await session.scalars(
            select(WikiPage)
            .where(WikiPage.id.in_(page_ids))
            .order_by(WikiPage.id)
            .with_for_update()
        )
        return {page.id: page for page in rows}

    @staticmethod
    async def _lock_generation(
        session: AsyncSession, generation_page_id: UUID
    ) -> GeneratedPage | None:
        return await session.scalar(
            select(GeneratedPage)
            .where(GeneratedPage.page_id == generation_page_id)
            .with_for_update()
        )

    @staticmethod
    async def _lock_binding(
        session: AsyncSession, page_id: UUID
    ) -> PageGenerationBinding | None:
        return await session.scalar(
            select(PageGenerationBinding)
            .where(PageGenerationBinding.page_id == page_id)
            .with_for_update()
        )

    @staticmethod
    def _require_completed_binding(
        operation: ReviewOperation,
        binding: PageGenerationBinding | None,
        generation: GeneratedPage,
    ) -> None:
        if (
            binding is None
            or binding.operation_id != operation.operation_id
            or binding.generation_page_id != operation.generation_page_id
            or binding.reviewed_sha256 != operation.reviewed_sha256
            or operation.completed_at is None
        ):
            raise GenerationConflictError("Completed review is no longer the current page binding")
        if operation.page_id == operation.generation_page_id and (
            generation.reviewed_at is None
            or generation.reviewed_sha256 != operation.reviewed_sha256
            or generation.reviewed_at != binding.reviewed_at
        ):
            raise GenerationConflictError(
                "Reviewed generation projection does not match its binding"
            )

    @staticmethod
    def _operation_matches(operation: ReviewOperation, fields: dict[str, Any]) -> bool:
        return all(getattr(operation, key) == value for key, value in fields.items())

    @staticmethod
    def _operation_snapshot(operation: ReviewOperation) -> dict[str, Any]:
        return {
            "operation_id": str(operation.operation_id),
            "page_id": str(operation.page_id),
            "generation_page_id": str(operation.generation_page_id),
            "expected_page_sha256": operation.expected_page_sha256,
            "expected_generation_sha256": operation.expected_generation_sha256,
            "reviewed_sha256": operation.reviewed_sha256,
            "state": operation.state,
            "created_at": ReviewRepository._iso(operation.created_at),
            "completed_at": ReviewRepository._iso(operation.completed_at),
        }

    @staticmethod
    def _binding_snapshot(binding: PageGenerationBinding) -> dict[str, Any]:
        return {
            "generation_page_id": str(binding.generation_page_id),
            "reviewed_sha256": binding.reviewed_sha256,
            "reviewed_at": ReviewRepository._iso(binding.reviewed_at),
            "operation_id": str(binding.operation_id),
        }

    @staticmethod
    def _iso(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _validate_ids(operation_id: UUID, page_id: UUID, generation_page_id: UUID) -> None:
        if not all(
            isinstance(value, UUID) for value in (operation_id, page_id, generation_page_id)
        ):
            raise ValueError("review operation and page identifiers must be UUIDs")

    @staticmethod
    def _validate_hashes(**hashes: str) -> None:
        if any(
            not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value)
            for value in hashes.values()
        ):
            raise ValueError("review hashes must be lowercase SHA-256 digests")
