"""Exclusive publication of derived evidence and newly generated Wiki files.

The caller must hold the Wiki service lock. Generation never replaces an
existing page: retrying the same retained result is safe only if bytes match.
"""

from collections.abc import Sequence
from uuid import UUID

from knowgrain.wiki_files import (
    MAX_PAGE_BYTES,
    WikiConflictError,
    WikiFile,
    WikiFileStore,
    WikiValidationError,
    parse_wiki,
)


class GenerationFileStore:
    def __init__(self, files: WikiFileStore) -> None:
        self.files = files

    def publish(
        self,
        page_id: UUID,
        markdown: str,
        evidence_pages: Sequence[tuple[UUID, str]],
    ) -> WikiFile:
        page_id = UUID(str(page_id))
        page_path = f"Wiki/Drafts/{page_id}.md"
        proposed = parse_wiki(markdown, page_path)
        if proposed.page_id != page_id or proposed.status != "draft":
            raise WikiValidationError("Generated page must match its reserved draft identity")
        if len(evidence_pages) > 24 or len({str(item[0]) for item in evidence_pages}) != len(evidence_pages):
            raise WikiValidationError("Generated evidence page set is invalid")

        prepared: list[tuple[str, bytes]] = []
        for evidence_id, text in evidence_pages:
            relative = f"Sources/Evidence/{UUID(str(evidence_id))}.md"
            encoded = text.encode("utf-8", errors="strict")
            if len(encoded) > MAX_PAGE_BYTES:
                raise WikiValidationError("Generated evidence page exceeds the supported size")
            self.files.vault.resolve(relative)
            prepared.append((relative, encoded))

        scan = self.files.scan()
        if not scan.complete:
            raise WikiConflictError("Wiki scan is incomplete", current=None, code="scan_incomplete", diff="")
        if any(issue.code == "duplicate_id" for issue in scan.issues):
            raise WikiConflictError("Wiki identity is ambiguous", current=None, code="duplicate_id", diff="")
        existing = next((page for page in scan.pages if page.page_id == page_id), None)
        if existing is not None and existing.content_sha256 != proposed.content_sha256:
            raise WikiConflictError(
                "Generated page was edited; automatic replacement is forbidden",
                current=existing, code="generation_modified", diff="",
            )

        # Evidence is derivative but still never silently overwrites another
        # editor's bytes. Orphan evidence after failure is safe to retain.
        for relative, encoded in prepared:
            destination = self.files.vault.resolve(relative)
            self.files._ensure_parent(relative)
            self.files._publish_exclusive_bytes(destination, encoded)

        if existing is not None:
            return existing
        destination = self.files.vault.resolve(page_path)
        self.files._ensure_parent(page_path)
        self.files._publish_exclusive(destination, markdown.encode("utf-8"))
        return proposed
