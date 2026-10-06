from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class VaultBinding(Base):
    __tablename__ = "vault_binding"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_vault_binding_singleton_id"),
        UniqueConstraint("binding_id", name="uq_vault_binding_binding_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    root_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SourceDocument(Base):
    __tablename__ = "source_document"
    __table_args__ = (
        CheckConstraint("state IN ('active', 'deleted')", name="ck_source_document_state"),
        CheckConstraint(
            "lifecycle_version >= 0", name="ck_source_document_lifecycle_version_nonnegative"
        ),
        ForeignKeyConstraint(
            ["latest_revision_id", "id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_source_document_latest_revision_source",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["current_revision_id", "id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_source_document_current_revision_source",
            use_alter=True,
        ),
        Index("ix_source_document_state_created", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    lifecycle_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    latest_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceRevision(Base):
    __tablename__ = "source_revision"
    __table_args__ = (
        Index("ix_source_revision_indexed_generation", "indexed_generation_id"),
        UniqueConstraint("source_id", "sha256", name="uq_source_revision_source_sha256"),
        UniqueConstraint("id", "source_id", name="uq_source_revision_id_source"),
        CheckConstraint(
            "index_state IN ('queued', 'indexing', 'ready', 'failed')",
            name="ck_source_revision_index_state",
        ),
        CheckConstraint("length(sha256) = 64", name="ck_source_revision_sha256_length"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_revision_sha256_hex"),
        Index("ix_source_revision_source_created", "source_id", "created_at"),
        Index("ix_source_revision_index_state", "index_state"),
        Index("ix_source_revision_vault_path", "vault_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_document.id", ondelete="RESTRICT"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    vault_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    index_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    parsed_text_sha256: Mapped[str | None] = mapped_column(String(64))
    parsed_segments: Mapped[list[dict] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    indexed_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "core_generation.id", name="fk_source_revision_core_generation", ondelete="RESTRICT"
        ),
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_generation", "generation_id"),
        CheckConstraint("claim_fence >= 0", name="ck_job_claim_fence_nonnegative"),
        CheckConstraint(
            "execution_epoch IS NULL OR execution_epoch >= 0",
            name="ck_job_execution_epoch_nonnegative",
        ),
        UniqueConstraint("revision_id", "kind", name="uq_job_revision_kind"),
        CheckConstraint("kind = 'index'", name="ck_job_kind"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')", name="ck_job_state"
        ),
        CheckConstraint("attempts >= 0", name="ck_job_attempts_nonnegative"),
        Index("ix_job_state_lease_created", "state", "lease_until", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="index", server_default="index"
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_revision.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    force_rebuild: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    cleanup_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("core_generation.id", name="fk_job_core_generation", ondelete="RESTRICT"),
    )
    execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(255))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CoreMaintenanceJob(Base):
    """Durable cleanup request for one deleted source revision lifecycle."""

    __tablename__ = "core_maintenance_job"
    __table_args__ = (
        Index("ix_core_maintenance_generation", "generation_id"),
        CheckConstraint("claim_fence >= 0", name="ck_core_maintenance_claim_fence_nonnegative"),
        CheckConstraint(
            "execution_epoch IS NULL OR execution_epoch >= 0",
            name="ck_core_maintenance_execution_epoch_nonnegative",
        ),
        ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_core_maintenance_revision_source",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "revision_id", "lifecycle_version", name="uq_core_maintenance_revision_lifecycle"
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_core_maintenance_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_core_maintenance_attempts_nonnegative"),
        CheckConstraint("lifecycle_version >= 0", name="ck_core_maintenance_lifecycle_nonnegative"),
        Index("ix_core_maintenance_state_lease_created", "state", "lease_until", "created_at"),
        Index(
            "ix_core_maintenance_source_lifecycle_created",
            "source_id",
            "lifecycle_version",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lifecycle_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "core_generation.id",
            name="fk_core_maintenance_job_core_generation",
            ondelete="RESTRICT",
        ),
    )
    execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(255))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4000))
    cleanup_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SourceFileOperation(Base):
    """Durable archive or restore intent for one source lifecycle."""

    __tablename__ = "source_file_operation"
    __table_args__ = (
        Index("ix_source_file_operation_generation", "generation_id"),
        CheckConstraint(
            "claim_fence >= 0", name="ck_source_file_operation_claim_fence_nonnegative"
        ),
        CheckConstraint(
            "execution_epoch IS NULL OR execution_epoch >= 0",
            name="ck_source_file_operation_execution_epoch_nonnegative",
        ),
        CheckConstraint("kind IN ('archive', 'restore')", name="ck_source_file_operation_kind"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_source_file_operation_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_source_file_operation_attempts_nonnegative"),
        CheckConstraint(
            "lifecycle_version >= 0", name="ck_source_file_operation_lifecycle_nonnegative"
        ),
        ForeignKeyConstraint(
            ["source_id"],
            ["source_document.id"],
            name="fk_source_file_operation_source_id_source_document",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "source_id",
            "lifecycle_version",
            "kind",
            name="uq_source_file_operation_source_lifecycle_kind",
        ),
        Index(
            "ix_source_file_operation_state_lease_created",
            "state",
            "lease_until",
            "created_at",
        ),
        Index(
            "ix_source_file_operation_source_lifecycle_created",
            "source_id",
            "lifecycle_version",
            "created_at",
        ),
        Index(
            "uq_source_file_operation_pending_source",
            "source_id",
            unique=True,
            postgresql_where=sql_text("state IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lifecycle_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    manifest: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "core_generation.id",
            name="fk_source_file_operation_core_generation",
            ondelete="RESTRICT",
        ),
    )
    execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(255))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4000))
    expected_latest_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    verified_current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WikiPage(Base):
    """Rebuildable identity and metadata projection for a Vault Markdown page."""

    __tablename__ = "wiki_page"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'reviewed')", name="ck_wiki_page_status"),
        CheckConstraint("length(content_sha256) = 64", name="ck_wiki_page_sha256_length"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_wiki_page_sha256_hex"),
        Index("ix_wiki_page_present", "present"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    vault_path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    present: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PageLink(Base):
    """Parsed wiki link projection. The Markdown body remains in the Vault."""

    __tablename__ = "page_link"
    __table_args__ = (
        Index("ix_page_link_to_page", "to_page_id", "from_page_id"),
        Index("ix_page_link_from_page", "from_page_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wiki_page.id", name="fk_page_link_from_page_id_wiki_page", ondelete="CASCADE"),
        nullable=False,
    )
    to_page_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wiki_page.id", name="fk_page_link_to_page_id_wiki_page", ondelete="SET NULL"),
    )
    target: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    embed: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    line: Mapped[int] = mapped_column(Integer, nullable=False)


class GenerationJob(Base):
    """Durable generation request and retained model result."""

    __tablename__ = "generation_job"
    __table_args__ = (
        Index("ix_generation_job_generation", "generation_id"),
        CheckConstraint("claim_fence >= 0", name="ck_generation_job_claim_fence_nonnegative"),
        CheckConstraint(
            "execution_epoch IS NULL OR execution_epoch >= 0",
            name="ck_generation_job_execution_epoch_nonnegative",
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_generation_job_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_generation_job_attempts_nonnegative"),
        CheckConstraint(
            "(target_page_id IS NULL) = (target_sha256 IS NULL)",
            name="ck_generation_job_target_pair",
        ),
        CheckConstraint(
            "target_sha256 IS NULL OR (length(target_sha256) = 64 AND target_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generation_job_target_sha256",
        ),
        CheckConstraint(
            "output_sha256 IS NULL OR (length(output_sha256) = 64 AND output_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generation_job_output_sha256",
        ),
        UniqueConstraint("output_page_id", name="uq_generation_job_output_page_id"),
        Index("ix_generation_job_state_lease_created", "state", "lease_until", "created_at"),
        Index("ix_generation_job_target_page", "target_page_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    target_page_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "wiki_page.id", name="fk_generation_job_target_page_id_wiki_page", ondelete="RESTRICT"
        ),
    )
    target_sha256: Mapped[str | None] = mapped_column(String(64))
    output_page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    phase: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "core_generation.id", name="fk_generation_job_core_generation", ondelete="RESTRICT"
        ),
    )
    execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(255))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EvidenceRef(Base):
    """Immutable, revision-bound evidence retained for generation and review."""

    __tablename__ = "evidence_ref"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_evidence_ref_revision_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "revision_id",
            "chunk_id",
            "excerpt_sha256",
            name="uq_evidence_ref_revision_chunk_excerpt",
        ),
        CheckConstraint("length(source_sha256) = 64", name="ck_evidence_ref_source_sha256_length"),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_ref_source_sha256_hex"
        ),
        CheckConstraint(
            "length(parsed_text_sha256) = 64", name="ck_evidence_ref_parsed_sha256_length"
        ),
        CheckConstraint(
            "parsed_text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_ref_parsed_sha256_hex"
        ),
        CheckConstraint(
            "length(excerpt_sha256) = 64", name="ck_evidence_ref_excerpt_sha256_length"
        ),
        CheckConstraint(
            "excerpt_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_ref_excerpt_sha256_hex"
        ),
        CheckConstraint('start >= 0 AND "end" > start', name="ck_evidence_ref_offsets"),
        CheckConstraint('length(excerpt) = "end" - start', name="ck_evidence_ref_excerpt_length"),
        CheckConstraint("page IS NULL OR page > 0", name="ck_evidence_ref_page_positive"),
        Index("ix_evidence_ref_source_revision", "source_id", "revision_id"),
        Index("ix_evidence_ref_chunk", "chunk_id"),
    )

    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    vault_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(512), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GeneratedPage(Base):
    """Durable generation manifest attached to a projected Wiki identity."""

    __tablename__ = "generated_page"
    __table_args__ = (
        CheckConstraint(
            "length(generated_sha256) = 64 AND generated_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_generated_page_generated_sha256",
        ),
        CheckConstraint(
            "(proposal_target_page_id IS NULL) = (proposal_target_sha256 IS NULL)",
            name="ck_generated_page_proposal_target_pair",
        ),
        CheckConstraint(
            "proposal_target_sha256 IS NULL OR (length(proposal_target_sha256) = 64 AND proposal_target_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generated_page_proposal_target_sha256",
        ),
        CheckConstraint(
            "reviewed_sha256 IS NULL OR (length(reviewed_sha256) = 64 AND reviewed_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generated_page_reviewed_sha256",
        ),
        CheckConstraint(
            "(reviewed_at IS NULL) = (reviewed_sha256 IS NULL)",
            name="ck_generated_page_review_pair",
        ),
        UniqueConstraint("generation_job_id", name="uq_generated_page_generation_job_id"),
        Index("ix_generated_page_proposal_target", "proposal_target_page_id"),
    )

    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wiki_page.id", name="fk_generated_page_page_id_wiki_page", ondelete="RESTRICT"),
        primary_key=True,
    )
    generation_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "generation_job.id", name="fk_generated_page_job_id_generation_job", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    draft: Mapped[dict] = mapped_column(JSONB, nullable=False)
    generated_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_target_page_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "wiki_page.id", name="fk_generated_page_proposal_target_wiki_page", ondelete="RESTRICT"
        ),
    )
    proposal_target_sha256: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PageEvidence(Base):
    """Claim-level references from generated pages to immutable evidence rows."""

    __tablename__ = "page_evidence"
    __table_args__ = (Index("ix_page_evidence_evidence_page", "evidence_id", "page_id"),)

    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "generated_page.page_id",
            name="fk_page_evidence_page_id_generated_page",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "evidence_ref.evidence_id", name="fk_page_evidence_evidence_ref", ondelete="RESTRICT"
        ),
        primary_key=True,
    )
    claim_key: Mapped[str] = mapped_column(String(64), primary_key=True)


class ReviewOperation(Base):
    """Immutable intent and completion state for one explicit review action."""

    __tablename__ = "review_operation"
    __table_args__ = (
        CheckConstraint(
            "length(expected_page_sha256) = 64 AND expected_page_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_operation_expected_page_sha256",
        ),
        CheckConstraint(
            "length(expected_generation_sha256) = 64 AND expected_generation_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_operation_expected_generation_sha256",
        ),
        CheckConstraint(
            "length(reviewed_sha256) = 64 AND reviewed_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_operation_reviewed_sha256",
        ),
        CheckConstraint("state IN ('prepared', 'completed')", name="ck_review_operation_state"),
        CheckConstraint(
            "(state = 'completed') = (completed_at IS NOT NULL)",
            name="ck_review_operation_completed_pair",
        ),
        Index("ix_review_operation_page", "page_id"),
        Index("ix_review_operation_generation", "generation_page_id"),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "wiki_page.id", name="fk_review_operation_page_id_wiki_page", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    generation_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "generated_page.page_id",
            name="fk_review_operation_generation_page_id_generated_page",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    expected_page_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_generation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="prepared", server_default="prepared"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QueryJob(Base):
    """Durable user question and immutable successful answer snapshot."""

    __tablename__ = "query_job"
    __table_args__ = (
        Index("ix_query_job_generation", "generation_id"),
        CheckConstraint("claim_fence >= 0", name="ck_query_job_claim_fence_nonnegative"),
        CheckConstraint(
            "execution_epoch IS NULL OR execution_epoch >= 0",
            name="ck_query_job_execution_epoch_nonnegative",
        ),
        CheckConstraint(
            "length(question) BETWEEN 1 AND 1000 AND length(trim(question)) > 0",
            name="ck_query_job_question_length",
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_query_job_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_query_job_attempts_nonnegative"),
        CheckConstraint(
            "(state = 'succeeded') = (result IS NOT NULL)",
            name="ck_query_job_result_state",
        ),
        Index("ix_query_job_state_lease_created", "state", "lease_until", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    question: Mapped[str] = mapped_column(String(1000), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("core_generation.id", name="fk_query_job_core_generation", ondelete="RESTRICT"),
    )
    execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(255))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PageGenerationBinding(Base):
    """Current reviewed Wiki identity mapped to its immutable generation manifest."""

    __tablename__ = "page_generation_binding"
    __table_args__ = (
        CheckConstraint(
            "length(reviewed_sha256) = 64 AND reviewed_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_page_generation_binding_reviewed_sha256",
        ),
        Index("ix_page_generation_binding_generation", "generation_page_id"),
    )

    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "wiki_page.id",
            name="fk_page_generation_binding_page_id_wiki_page",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    generation_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "generated_page.page_id",
            name="fk_page_generation_binding_generation_page_id_generated_page",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reviewed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "review_operation.operation_id",
            name="fk_page_generation_binding_operation_id_review_operation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


class CoreGeneration(Base):
    __tablename__ = "core_generation"
    __table_args__ = (
        UniqueConstraint("workspace", name="uq_core_generation_workspace"),
        UniqueConstraint("working_dir", name="uq_core_generation_working_dir"),
        UniqueConstraint("vector_model_name", name="uq_core_generation_vector_model_name"),
        CheckConstraint(
            "config_status IN ('legacy_unverified', 'sealed')",
            name="ck_core_generation_config_status",
        ),
        CheckConstraint(
            "(config_status = 'sealed' AND canonical_profile IS NOT NULL AND content_embedding_fingerprint IS NOT NULL AND graph_write_fingerprint IS NOT NULL AND llm_fingerprint IS NOT NULL AND snapshot_fingerprint IS NOT NULL AND vector_model_name IS NOT NULL) OR (config_status = 'legacy_unverified' AND canonical_profile IS NULL AND content_embedding_fingerprint IS NULL AND graph_write_fingerprint IS NULL AND llm_fingerprint IS NULL AND snapshot_fingerprint IS NULL)",
            name="ck_core_generation_profile_status",
        ),
        CheckConstraint(
            "vector_model_name IS NULL OR vector_model_name ~ '^kg_[0-9a-f]{24}$'",
            name="ck_core_generation_vector_model_name",
        ),
        CheckConstraint(
            "content_embedding_fingerprint IS NULL OR content_embedding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_content_embedding_fingerprint",
        ),
        CheckConstraint(
            "graph_write_fingerprint IS NULL OR graph_write_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_graph_write_fingerprint",
        ),
        CheckConstraint(
            "llm_fingerprint IS NULL OR llm_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_llm_fingerprint",
        ),
        CheckConstraint(
            "snapshot_fingerprint IS NULL OR snapshot_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_snapshot_fingerprint",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace: Mapped[str] = mapped_column(String(128), nullable=False)
    working_dir: Mapped[str] = mapped_column(String(4096), nullable=False)
    vector_model_name: Mapped[str | None] = mapped_column(String(27))
    config_status: Mapped[str] = mapped_column(String(24), nullable=False)
    canonical_profile: Mapped[str | None] = mapped_column(Text)
    content_embedding_fingerprint: Mapped[str | None] = mapped_column(String(64))
    graph_write_fingerprint: Mapped[str | None] = mapped_column(String(64))
    llm_fingerprint: Mapped[str | None] = mapped_column(String(64))
    snapshot_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoreSelector(Base):
    __tablename__ = "core_selector"
    __table_args__ = (
        Index("ix_core_selector_active_generation", "active_generation_id"),
        Index("ix_core_selector_pending_rebuild", "pending_rebuild_id"),
        CheckConstraint("id = 1", name="ck_core_selector_singleton_id"),
        CheckConstraint("version >= 0", name="ck_core_selector_version_nonnegative"),
        CheckConstraint(
            "execution_epoch >= 0", name="ck_core_selector_execution_epoch_nonnegative"
        ),
        CheckConstraint(
            "pending_rebuild_id IS NULL OR frozen", name="ck_core_selector_pending_frozen"
        ),
        ForeignKeyConstraint(
            ["active_generation_id"],
            ["core_generation.id"],
            name="fk_core_selector_active_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["pending_rebuild_id"],
            ["rebuild_operation.id"],
            name="fk_core_selector_pending_rebuild",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    execution_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    active_generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    pending_rebuild_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    frozen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    restart_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RebuildOperation(Base):
    __tablename__ = "rebuild_operation"
    __table_args__ = (
        Index("ix_rebuild_operation_old_generation", "old_generation_id"),
        Index("ix_rebuild_operation_state_lease_created", "state", "lease_until", "created_at"),
        CheckConstraint(
            "retry_from_version IS NULL OR retry_from_version >= 0",
            name="ck_rebuild_operation_retry_from_version_nonnegative",
        ),
        ForeignKeyConstraint(
            ["old_generation_id"],
            ["core_generation.id"],
            name="fk_rebuild_operation_old_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_generation_id"],
            ["core_generation.id"],
            name="fk_rebuild_operation_target_generation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("target_generation_id", name="uq_rebuild_operation_target_generation"),
        UniqueConstraint("id", "target_generation_id", name="uq_rebuild_operation_id_target"),
        CheckConstraint(
            "old_generation_id IS NULL OR old_generation_id != target_generation_id",
            name="ck_rebuild_operation_distinct_generations",
        ),
        CheckConstraint(
            "state IN ('queued', 'preparing', 'building', 'verifying', 'succeeded', 'failed')",
            name="ck_rebuild_operation_state",
        ),
        CheckConstraint(
            "(snapshot_sha256 IS NULL AND snapshot_count IS NULL AND snapshot_execution_epoch IS NULL) OR (snapshot_sha256 IS NOT NULL AND snapshot_count IS NOT NULL AND snapshot_execution_epoch IS NOT NULL)",
            name="ck_rebuild_operation_snapshot_group",
        ),
        CheckConstraint(
            "(claim_token IS NULL AND lease_owner IS NULL AND lease_until IS NULL) OR (claim_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_rebuild_operation_lease_group",
        ),
        CheckConstraint(
            "expected_selector_version >= 0",
            name="ck_rebuild_operation_expected_selector_version_nonnegative",
        ),
        CheckConstraint("version >= 0", name="ck_rebuild_operation_version_nonnegative"),
        CheckConstraint("attempts >= 0", name="ck_rebuild_operation_attempts_nonnegative"),
        CheckConstraint("claim_fence >= 0", name="ck_rebuild_operation_claim_fence_nonnegative"),
        CheckConstraint(
            "snapshot_count IS NULL OR snapshot_count >= 0",
            name="ck_rebuild_operation_snapshot_count_nonnegative",
        ),
        CheckConstraint(
            "snapshot_execution_epoch IS NULL OR snapshot_execution_epoch >= 0",
            name="ck_rebuild_operation_snapshot_execution_epoch_nonnegative",
        ),
        CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rebuild_operation_request_sha256"
        ),
        CheckConstraint(
            "snapshot_sha256 IS NULL OR snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rebuild_operation_snapshot_sha256",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    old_generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    target_generation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expected_selector_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    retry_from_version: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    vault_binding_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    vault_root_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_count: Mapped[int | None] = mapped_column(Integer)
    snapshot_execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RebuildItem(Base):
    __tablename__ = "rebuild_item"
    __table_args__ = (
        Index("ix_rebuild_item_operation_generation", "operation_id", "generation_id"),
        Index("ix_rebuild_item_revision_source", "revision_id", "source_id"),
        Index(
            "ix_rebuild_item_operation_state_lease_source",
            "operation_id",
            "state",
            "lease_until",
            "source_id",
        ),
        CheckConstraint(
            "state != 'verified' OR (verified_at IS NOT NULL AND parsed_text_sha256 IS NOT NULL AND parsed_segments IS NOT NULL AND physical_indexed_at IS NOT NULL)",
            name="ck_rebuild_item_verified_metadata",
        ),
        ForeignKeyConstraint(
            ["operation_id", "generation_id"],
            ["rebuild_operation.id", "rebuild_operation.target_generation_id"],
            name="fk_rebuild_item_operation_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_rebuild_item_revision_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("operation_id", "source_id", name="uq_rebuild_item_operation_source"),
        UniqueConstraint("operation_id", "revision_id", name="uq_rebuild_item_operation_revision"),
        CheckConstraint(
            "state IN ('queued', 'running', 'failed', 'verified')", name="ck_rebuild_item_state"
        ),
        CheckConstraint(
            "(claim_token IS NULL AND lease_owner IS NULL AND lease_until IS NULL) OR (claim_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_rebuild_item_lease_group",
        ),
        CheckConstraint(
            "(parent_claim_token IS NULL) = (parent_claim_fence IS NULL)",
            name="ck_rebuild_item_parent_claim_pair",
        ),
        CheckConstraint(
            "claim_token IS NULL OR parent_claim_token IS NOT NULL",
            name="ck_rebuild_item_claim_parent",
        ),
        CheckConstraint(
            "lifecycle_version >= 0", name="ck_rebuild_item_lifecycle_version_nonnegative"
        ),
        CheckConstraint("attempts >= 0", name="ck_rebuild_item_attempts_nonnegative"),
        CheckConstraint("claim_fence >= 0", name="ck_rebuild_item_claim_fence_nonnegative"),
        CheckConstraint(
            "parent_claim_fence IS NULL OR parent_claim_fence >= 0",
            name="ck_rebuild_item_parent_claim_fence_nonnegative",
        ),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rebuild_item_source_sha256"),
        CheckConstraint(
            "snapshot_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rebuild_item_snapshot_sha256"
        ),
        CheckConstraint(
            "original_parsed_text_sha256 IS NULL OR original_parsed_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rebuild_item_original_parsed_text_sha256",
        ),
        CheckConstraint(
            "parsed_text_sha256 IS NULL OR parsed_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rebuild_item_parsed_text_sha256",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lifecycle_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    vault_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_parsed_text_sha256: Mapped[str | None] = mapped_column(String(64))
    original_parsed_segments: Mapped[list[dict] | None] = mapped_column(JSONB)
    original_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    parent_claim_fence: Mapped[int | None] = mapped_column(BigInteger)
    cleanup_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    parsed_text_sha256: Mapped[str | None] = mapped_column(String(64))
    parsed_segments: Mapped[list[dict] | None] = mapped_column(JSONB)
    physical_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CoreGenerationRevision(Base):
    __tablename__ = "core_generation_revision"
    __table_args__ = (
        Index("ix_core_generation_revision_revision_source", "revision_id", "source_id"),
        Index(
            "ix_core_generation_revision_generation_source_state",
            "generation_id",
            "source_id",
            "state",
        ),
        CheckConstraint(
            "state != 'verified' OR (verified_at IS NOT NULL AND parsed_text_sha256 IS NOT NULL AND parsed_segments IS NOT NULL AND physical_indexed_at IS NOT NULL)",
            name="ck_core_generation_revision_verified_metadata",
        ),
        CheckConstraint(
            "state != 'cleaned' OR cleaned_at IS NOT NULL",
            name="ck_core_generation_revision_cleaned_metadata",
        ),
        ForeignKeyConstraint(
            ["generation_id"],
            ["core_generation.id"],
            name="fk_core_generation_revision_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_core_generation_revision_revision_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('write_intent', 'indexing', 'failed', 'verified', 'cleaned')",
            name="ck_core_generation_revision_state",
        ),
        CheckConstraint(
            "claim_fence >= 0", name="ck_core_generation_revision_claim_fence_nonnegative"
        ),
        CheckConstraint(
            "parsed_text_sha256 IS NULL OR parsed_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_revision_parsed_text_sha256",
        ),
    )

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="write_intent", server_default="write_intent"
    )
    write_intent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    parsed_text_sha256: Mapped[str | None] = mapped_column(String(64))
    parsed_segments: Mapped[list[dict] | None] = mapped_column(JSONB)
    cleanup_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    physical_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4000))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
