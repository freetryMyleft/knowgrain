"""Persist source archive and restore intents.

Revision ID: 0012_source_file_operations
Revises: 0011_m5_core_maintenance
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0012_source_file_operations"
down_revision: Union[str, None] = "0011_m5_core_maintenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_file_operation",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("lifecycle_version", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
        sa.Column("expected_latest_revision_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("verified_current_revision_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('archive', 'restore')", name="ck_source_file_operation_kind"
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_source_file_operation_state",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_source_file_operation_attempts_nonnegative"
        ),
        sa.CheckConstraint(
            "lifecycle_version >= 0",
            name="ck_source_file_operation_lifecycle_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source_document.id"],
            name="fk_source_file_operation_source_id_source_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_file_operation"),
        sa.UniqueConstraint(
            "source_id",
            "lifecycle_version",
            "kind",
            name="uq_source_file_operation_source_lifecycle_kind",
        ),
    )
    op.create_index(
        "ix_source_file_operation_state_lease_created",
        "source_file_operation",
        ["state", "lease_until", "created_at"],
    )
    op.create_index(
        "ix_source_file_operation_source_lifecycle_created",
        "source_file_operation",
        ["source_id", "lifecycle_version", "created_at"],
    )
    op.create_index(
        "uq_source_file_operation_pending_source",
        "source_file_operation",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )

    # Preserve cleanup completion as the durable prerequisite. If its source
    # has an invalid or oversized archive manifest, retain an explicit failed
    # archive intent so maintenance completion itself cannot be rolled back.
    op.execute(
        sa.text(
            r"""
            INSERT INTO source_file_operation (
                id, source_id, lifecycle_version, kind, state, manifest, attempts,
                error, created_at, updated_at
            )
            SELECT
                gen_random_uuid(), source.id, source.lifecycle_version, 'archive',
                CASE
                    WHEN count(revision.id) BETWEEN 1 AND 10000
                     AND bool_and(
                         revision.vault_path ~ (
                             '^Sources/Files/' || source.id::text || '/' ||
                             revision.id::text || '\.(md|markdown|txt|pdf|docx)$'
                         )
                     )
                    THEN 'queued'
                    ELSE 'failed'
                END,
                jsonb_agg(
                    jsonb_build_object(
                        'revision_id', revision.id::text,
                        'vault_path', revision.vault_path,
                        'sha256', revision.sha256
                    ) ORDER BY revision.id
                ),
                0,
                CASE
                    WHEN count(revision.id) BETWEEN 1 AND 10000
                     AND bool_and(
                         revision.vault_path ~ (
                             '^Sources/Files/' || source.id::text || '/' ||
                             revision.id::text || '\.(md|markdown|txt|pdf|docx)$'
                         )
                     )
                    THEN NULL
                    ELSE 'Source archive manifest is invalid or exceeds the supported limit'
                END,
                clock_timestamp(), clock_timestamp()
            FROM source_document AS source
            JOIN source_revision AS revision ON revision.source_id = source.id
            LEFT JOIN core_maintenance_job AS maintenance
              ON maintenance.source_id = source.id
             AND maintenance.revision_id = revision.id
             AND maintenance.lifecycle_version = source.lifecycle_version
            WHERE source.state = 'deleted'
            GROUP BY source.id, source.lifecycle_version
            HAVING count(revision.id) > 0
               AND count(revision.id) = count(maintenance.id)
               AND bool_and(maintenance.state = 'succeeded')
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "uq_source_file_operation_pending_source", table_name="source_file_operation"
    )
    op.drop_index(
        "ix_source_file_operation_source_lifecycle_created", table_name="source_file_operation"
    )
    op.drop_index(
        "ix_source_file_operation_state_lease_created", table_name="source_file_operation"
    )
    op.drop_table("source_file_operation")
