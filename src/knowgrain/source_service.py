import asyncio
import hashlib
from pathlib import PurePosixPath
from uuid import UUID

from knowgrain.config import Settings
from knowgrain.source_repository import ImportResult, SourceRepository
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


class SourceService:
    def __init__(self, settings: Settings, repository: SourceRepository, vault: VaultStore):
        self.settings = settings
        self.repository = repository
        self.vault = vault

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
