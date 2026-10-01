"""Add durable evidence-backed generation and review records.

Revision ID: 0005_m3_generation
Revises: 0004_m2_link_identity
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_m3_generation"
down_revision: Union[str, None] = "0004_m2_link_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generation_job",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("target_page_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("target_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("phase", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_generation_job_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_generation_job_attempts_nonnegative"),
        sa.CheckConstraint(
            "(target_page_id IS NULL) = (target_sha256 IS NULL)",
            name="ck_generation_job_target_pair",
        ),
        sa.CheckConstraint(
            "target_sha256 IS NULL OR (length(target_sha256) = 64 AND target_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generation_job_target_sha256",
        ),
        sa.CheckConstraint(
            "output_sha256 IS NULL OR (length(output_sha256) = 64 AND output_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generation_job_output_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["wiki_page.id"],
            name="fk_generation_job_target_page_id_wiki_page",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_job"),
        sa.UniqueConstraint("output_page_id", name="uq_generation_job_output_page_id"),
    )
    op.create_index(
        "ix_generation_job_state_lease_created",
        "generation_job",
        ["state", "lease_until", "created_at"],
    )

    op.create_table(
        "evidence_ref",
        sa.Column("evidence_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("vault_path", sa.String(length=2048), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("parsed_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("chunk_id", sa.String(length=512), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("excerpt_sha256", sa.String(length=64), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(source_sha256) = 64", name="ck_evidence_ref_source_sha256_length"),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_ref_source_sha256_hex"),
        sa.CheckConstraint(
            "length(parsed_text_sha256) = 64", name="ck_evidence_ref_parsed_sha256_length"
        ),
        sa.CheckConstraint(
            "parsed_text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_ref_parsed_sha256_hex"
        ),
        sa.CheckConstraint("length(excerpt_sha256) = 64", name="ck_evidence_ref_excerpt_sha256_length"),
        sa.CheckConstraint("excerpt_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_ref_excerpt_sha256_hex"),
        sa.CheckConstraint("start >= 0 AND \"end\" > start", name="ck_evidence_ref_offsets"),
        sa.CheckConstraint("length(excerpt) = \"end\" - start", name="ck_evidence_ref_excerpt_length"),
        sa.CheckConstraint("page IS NULL OR page > 0", name="ck_evidence_ref_page_positive"),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_evidence_ref_revision_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_evidence_ref"),
        sa.UniqueConstraint(
            "revision_id",
            "chunk_id",
            "excerpt_sha256",
            name="uq_evidence_ref_revision_chunk_excerpt",
        ),
    )
    op.create_index("ix_evidence_ref_source_revision", "evidence_ref", ["source_id", "revision_id"])

    op.create_table(
        "generated_page",
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation_job_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("draft", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_sha256", sa.String(length=64), nullable=False),
        sa.Column("proposal_target_page_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("proposal_target_sha256", sa.String(length=64), nullable=True),
        sa.Column("model", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "length(generated_sha256) = 64 AND generated_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_generated_page_generated_sha256",
        ),
        sa.CheckConstraint(
            "(proposal_target_page_id IS NULL) = (proposal_target_sha256 IS NULL)",
            name="ck_generated_page_proposal_target_pair",
        ),
        sa.CheckConstraint(
            "proposal_target_sha256 IS NULL OR (length(proposal_target_sha256) = 64 AND proposal_target_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generated_page_proposal_target_sha256",
        ),
        sa.CheckConstraint(
            "reviewed_sha256 IS NULL OR (length(reviewed_sha256) = 64 AND reviewed_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_generated_page_reviewed_sha256",
        ),
        sa.CheckConstraint(
            "(reviewed_at IS NULL) = (reviewed_sha256 IS NULL)",
            name="ck_generated_page_review_pair",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"], ["wiki_page.id"],
            name="fk_generated_page_page_id_wiki_page", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_job.id"],
            name="fk_generated_page_job_id_generation_job", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_target_page_id"], ["wiki_page.id"],
            name="fk_generated_page_proposal_target_wiki_page", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("page_id", name="pk_generated_page"),
        sa.UniqueConstraint("generation_job_id", name="uq_generated_page_generation_job_id"),
    )

    op.create_table(
        "page_evidence",
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("evidence_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("claim_key", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["page_id"], ["generated_page.page_id"],
            name="fk_page_evidence_page_id_generated_page", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence_ref.evidence_id"],
            name="fk_page_evidence_evidence_ref", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("page_id", "evidence_id", "claim_key", name="pk_page_evidence"),
    )
    op.create_index("ix_page_evidence_evidence_page", "page_evidence", ["evidence_id", "page_id"])


def downgrade() -> None:
    op.drop_index("ix_page_evidence_evidence_page", table_name="page_evidence")
    op.drop_table("page_evidence")
    op.drop_table("generated_page")
    op.drop_index("ix_evidence_ref_source_revision", table_name="evidence_ref")
    op.drop_table("evidence_ref")
    op.drop_index("ix_generation_job_state_lease_created", table_name="generation_job")
    op.drop_table("generation_job")
