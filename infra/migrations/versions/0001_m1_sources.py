"""Create source import, revision, and indexing job tables.

Revision ID: 0001_m1_sources
Revises:
Create Date: 2026-09-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_m1_sources"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_document",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("latest_revision_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("current_revision_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("state IN ('active', 'deleted')", name="ck_source_document_state"),
        sa.PrimaryKeyConstraint("id", name="pk_source_document"),
    )
    op.create_index(
        "ix_source_document_state_created", "source_document", ["state", "created_at"]
    )
    op.create_table(
        "source_revision",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("vault_path", sa.String(length=2048), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("index_state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("parsed_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("parsed_segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "index_state IN ('queued', 'indexing', 'ready', 'failed')",
            name="ck_source_revision_index_state",
        ),
        sa.CheckConstraint("length(sha256) = 64", name="ck_source_revision_sha256_length"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_revision_sha256_hex"),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source_document.id"],
            name="fk_source_revision_source_id_source_document",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_revision"),
        sa.UniqueConstraint("id", "source_id", name="uq_source_revision_id_source"),
        sa.UniqueConstraint("source_id", "sha256", name="uq_source_revision_source_sha256"),
    )
    op.create_index(
        "ix_source_revision_source_created", "source_revision", ["source_id", "created_at"]
    )
    op.create_index("ix_source_revision_index_state", "source_revision", ["index_state"])
    op.create_table(
        "job",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="index", nullable=False),
        sa.Column("revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_job_attempts_nonnegative"),
        sa.CheckConstraint("kind = 'index'", name="ck_job_kind"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')", name="ck_job_state"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["source_revision.id"],
            name="fk_job_revision_id_source_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
        sa.UniqueConstraint("revision_id", "kind", name="uq_job_revision_kind"),
    )
    op.create_index("ix_job_state_lease_created", "job", ["state", "lease_until", "created_at"])
    op.create_foreign_key(
        "fk_source_document_latest_revision_source",
        "source_document",
        "source_revision",
        ["latest_revision_id", "id"],
        ["id", "source_id"],
    )
    op.create_foreign_key(
        "fk_source_document_current_revision_source",
        "source_document",
        "source_revision",
        ["current_revision_id", "id"],
        ["id", "source_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_source_document_current_revision_source", "source_document", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_source_document_latest_revision_source", "source_document", type_="foreignkey"
    )
    op.drop_index("ix_job_state_lease_created", table_name="job")
    op.drop_table("job")
    op.drop_index("ix_source_revision_index_state", table_name="source_revision")
    op.drop_index("ix_source_revision_source_created", table_name="source_revision")
    op.drop_table("source_revision")
    op.drop_index("ix_source_document_state_created", table_name="source_document")
    op.drop_table("source_document")
