import asyncio
import hashlib
from contextlib import suppress
from pathlib import PurePosixPath
from uuid import UUID

from knowgrain.config import Settings
from knowgrain.evidence_access import EvidenceAccess, EvidenceFileError
from knowgrain.source_repository import (
    ImportResult,
    SourceConflictError,
    SourceNotFoundError,
    SourceRepository,
)
from knowgrain.vault import VaultStore

SUPPORTED_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class InvalidUploadError(ValueError):
    pass


class SourceLifecycleUnavailableError(RuntimeError):
    """The source lifecycle service cannot safely complete a request."""


_SOURCE_FILE_CONFLICT_MESSAGE = (
    "Vault 中的原件缺失或已变化，无法恢复资料；请检查文件后重试"
)
_SOURCE_FILE_UNAVAILABLE_MESSAGE = (
    "Vault 原件暂不可安全读取，请检查 Vault 状态与权限后重试"
)


async def _thread_call_drained(function, *args):
    """Join Vault I/O threads before cancellation can release a runtime lock."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
            if not worker.done():
                continue
            with suppress(Exception, asyncio.CancelledError):
                worker.result()
            raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


class SourceService:
    def __init__(
        self,
        settings: Settings,
        repository: SourceRepository,
        vault: VaultStore,
        evidence_access: EvidenceAccess | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.vault = vault
        self.evidence_access = evidence_access or EvidenceAccess(vault)

    async def import_file(
        self, filename: str, content: bytes, *, source_id: UUID | None = None
    ) -> ImportResult:
        if not filename or len(filename) > 240 or "\x00" in filename:
            raise InvalidUploadError("文件名为空或过长")
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise InvalidUploadError("文件名不能包含路径")
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in SUPPORTED_MEDIA_TYPES:
            raise InvalidUploadError("支持的文件格式：Markdown、TXT、PDF、DOCX")
        if not content:
            raise InvalidUploadError("不能导入空文件")
        if len(content) > self.settings.max_upload_bytes:
            raise InvalidUploadError("文件超过上传大小限制")
        digest = hashlib.sha256(content).hexdigest()

        async def persist(document_id: UUID, revision_id: UUID) -> str:
            return await asyncio.to_thread(
                self.vault.write_source, document_id, revision_id, filename, content
            )

        return await self.repository.register_source(
            filename=filename,
            sha256=digest,
            media_type=SUPPORTED_MEDIA_TYPES[suffix],
            persist=persist,
            source_id=source_id,
        )

    async def soft_delete_source(
        self,
        source_id: UUID,
        *,
        expected_lifecycle_version: int,
        expected_latest_revision_id: UUID | None,
    ) -> dict:
        return await self.repository.soft_delete_source(
            source_id,
            expected_lifecycle_version=expected_lifecycle_version,
            expected_latest_revision_id=expected_latest_revision_id,
        )

    async def restore_source(
        self,
        source_id: UUID,
        *,
        expected_lifecycle_version: int,
        expected_latest_revision_id: UUID | None,
    ) -> dict:
        # Capture the pointers and matching revision metadata before reading files.
        # The repository compares the current pointer again under its transaction lock.
        snapshot = await self.repository.get_source(source_id)
        if snapshot is None:
            raise SourceNotFoundError(f"Source {source_id} does not exist")

        latest_id, latest_revision = self._revision_from_snapshot(
            snapshot, "latest_revision_id", "latest_revision"
        )
        current_id, current_revision = self._revision_from_snapshot(
            snapshot, "current_revision_id", "current_revision"
        )

        verified: set[UUID] = set()
        for revision_id, revision in (
            (latest_id, latest_revision),
            (current_id, current_revision),
        ):
            if revision_id is None or revision_id in verified:
                continue
            if revision is None:
                raise SourceConflictError(_SOURCE_FILE_CONFLICT_MESSAGE)
            path = revision.get("vault_path")
            digest = revision.get("sha256")
            try:
                await _thread_call_drained(
                    self.evidence_access.original_revision, path, digest
                )
            except EvidenceFileError as exc:
                if exc.code in {"missing", "conflict"}:
                    raise SourceConflictError(_SOURCE_FILE_CONFLICT_MESSAGE) from None
                raise SourceLifecycleUnavailableError(
                    _SOURCE_FILE_UNAVAILABLE_MESSAGE
                ) from None
            except (OSError, ValueError, TypeError):
                raise SourceLifecycleUnavailableError(
                    _SOURCE_FILE_UNAVAILABLE_MESSAGE
                ) from None
            verified.add(revision_id)

        return await self.repository.restore_source(
            source_id,
            expected_lifecycle_version=expected_lifecycle_version,
            expected_latest_revision_id=expected_latest_revision_id,
            verified_current_revision_id=current_id,
        )

    @staticmethod
    def _revision_from_snapshot(
        snapshot: dict, pointer_key: str, revision_key: str
    ) -> tuple[UUID | None, dict | None]:
        raw_id = snapshot.get(pointer_key)
        if raw_id is None:
            return None, None
        try:
            revision_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
        except (ValueError, TypeError, AttributeError):
            raise SourceConflictError(_SOURCE_FILE_CONFLICT_MESSAGE) from None
        revision = snapshot.get(revision_key)
        if not isinstance(revision, dict):
            return revision_id, None
        raw_revision_id = revision.get("id", revision.get("revision_id"))
        try:
            snapshot_revision_id = (
                raw_revision_id
                if isinstance(raw_revision_id, UUID)
                else UUID(str(raw_revision_id))
            )
        except (ValueError, TypeError, AttributeError):
            return revision_id, None
        if snapshot_revision_id != revision_id:
            return revision_id, None
        return revision_id, revision
