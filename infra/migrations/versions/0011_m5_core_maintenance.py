"""Persist Core cleanup and forced rebuild manifests.

Revision ID: 0011_m5_core_maintenance
Revises: 0010_m5_source_lifecycle
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0011_m5_core_maintenance"
down_revision: Union[str, None] = "0010_m5_source_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job",
        sa.Column("force_rebuild", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "job",
        sa.Column("cleanup_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "core_maintenance_job",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("lifecycle_version", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
        sa.Column(
            "cleanup_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_core_maintenance_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_core_maintenance_attempts_nonnegative"),
        sa.CheckConstraint(
            "lifecycle_version >= 0", name="ck_core_maintenance_lifecycle_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_core_maintenance_revision_source",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_core_maintenance_job"),
        sa.UniqueConstraint(
            "revision_id", "lifecycle_version", name="uq_core_maintenance_revision_lifecycle"
        ),
    )
    op.create_index(
        "ix_core_maintenance_state_lease_created",
        "core_maintenance_job",
        ["state", "lease_until", "created_at"],
    )
    op.create_index(
        "ix_core_maintenance_source_lifecycle_created",
        "core_maintenance_job",
        ["source_id", "lifecycle_version", "created_at"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO core_maintenance_job (
                id, source_id, revision_id, lifecycle_version, state, attempts,
                created_at, updated_at
            )
            SELECT gen_random_uuid(), source.id, revision.id, source.lifecycle_version,
                   'queued', 0, clock_timestamp(), clock_timestamp()
            FROM source_document AS source
            JOIN source_revision AS revision ON revision.source_id = source.id
            WHERE source.state = 'deleted'
            ON CONFLICT (revision_id, lifecycle_version) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_core_maintenance_source_lifecycle_created", table_name="core_maintenance_job"
    )
    op.drop_index("ix_core_maintenance_state_lease_created", table_name="core_maintenance_job")
    op.drop_table("core_maintenance_job")
    op.drop_column("job", "cleanup_chunk_ids")
    op.drop_column("job", "force_rebuild")
