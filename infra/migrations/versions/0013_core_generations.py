"""Add inert Core generation and rebuild ledgers without runtime activation.

Revision ID: 0013_core_generations
Revises: 0012_source_file_operations
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_core_generations"
down_revision = "0012_source_file_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add empty ledgers; existing rows are preserved without inferred identities.
    op.create_table(
        "core_generation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace", sa.String(length=128), nullable=False),
        sa.Column("working_dir", sa.String(length=4096), nullable=False),
        sa.Column("vector_model_name", sa.String(length=27), nullable=True),
        sa.Column("config_status", sa.String(length=24), nullable=False),
        sa.Column("canonical_profile", sa.Text(), nullable=True),
        sa.Column("content_embedding_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("graph_write_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("llm_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("snapshot_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(config_status = 'sealed' AND canonical_profile IS NOT NULL AND content_embedding_fingerprint IS NOT NULL AND graph_write_fingerprint IS NOT NULL AND llm_fingerprint IS NOT NULL AND snapshot_fingerprint IS NOT NULL AND vector_model_name IS NOT NULL) OR (config_status = 'legacy_unverified' AND canonical_profile IS NULL AND content_embedding_fingerprint IS NULL AND graph_write_fingerprint IS NULL AND llm_fingerprint IS NULL AND snapshot_fingerprint IS NULL)",
            name="ck_core_generation_profile_status",
        ),
        sa.CheckConstraint(
            "config_status IN ('legacy_unverified', 'sealed')",
            name="ck_core_generation_config_status",
        ),
        sa.CheckConstraint(
            "content_embedding_fingerprint IS NULL OR content_embedding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_content_embedding_fingerprint",
        ),
        sa.CheckConstraint(
            "graph_write_fingerprint IS NULL OR graph_write_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_graph_write_fingerprint",
        ),
        sa.CheckConstraint(
            "llm_fingerprint IS NULL OR llm_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_llm_fingerprint",
        ),
        sa.CheckConstraint(
            "snapshot_fingerprint IS NULL OR snapshot_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_snapshot_fingerprint",
        ),
        sa.CheckConstraint(
            "vector_model_name IS NULL OR vector_model_name ~ '^kg_[0-9a-f]{24}$'",
            name="ck_core_generation_vector_model_name",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vector_model_name", name="uq_core_generation_vector_model_name"),
        sa.UniqueConstraint("working_dir", name="uq_core_generation_working_dir"),
        sa.UniqueConstraint("workspace", name="uq_core_generation_workspace"),
    )
    op.create_table(
        "rebuild_operation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("old_generation_id", sa.Uuid(), nullable=True),
        sa.Column("target_generation_id", sa.Uuid(), nullable=False),
        sa.Column("expected_selector_version", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("retry_from_version", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("vault_binding_id", sa.Uuid(), nullable=False),
        sa.Column("vault_root_path", sa.String(length=4096), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=True),
        sa.Column("snapshot_count", sa.Integer(), nullable=True),
        sa.Column("snapshot_execution_epoch", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "retry_from_version IS NULL OR retry_from_version >= 0",
            name="ck_rebuild_operation_retry_from_version_nonnegative",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rebuild_operation_request_sha256"
        ),
        sa.CheckConstraint(
            "snapshot_sha256 IS NULL OR snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rebuild_operation_snapshot_sha256",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'preparing', 'building', 'verifying', 'succeeded', 'failed')",
            name="ck_rebuild_operation_state",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL AND lease_owner IS NULL AND lease_until IS NULL) OR (claim_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_rebuild_operation_lease_group",
        ),
        sa.CheckConstraint(
            "(snapshot_sha256 IS NULL AND snapshot_count IS NULL AND snapshot_execution_epoch IS NULL) OR (snapshot_sha256 IS NOT NULL AND snapshot_count IS NOT NULL AND snapshot_execution_epoch IS NOT NULL)",
            name="ck_rebuild_operation_snapshot_group",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_rebuild_operation_attempts_nonnegative"),
        sa.CheckConstraint("claim_fence >= 0", name="ck_rebuild_operation_claim_fence_nonnegative"),
        sa.CheckConstraint(
            "expected_selector_version >= 0",
            name="ck_rebuild_operation_expected_selector_version_nonnegative",
        ),
        sa.CheckConstraint(
            "old_generation_id IS NULL OR old_generation_id != target_generation_id",
            name="ck_rebuild_operation_distinct_generations",
        ),
        sa.CheckConstraint(
            "snapshot_count IS NULL OR snapshot_count >= 0",
            name="ck_rebuild_operation_snapshot_count_nonnegative",
        ),
        sa.CheckConstraint(
            "snapshot_execution_epoch IS NULL OR snapshot_execution_epoch >= 0",
            name="ck_rebuild_operation_snapshot_execution_epoch_nonnegative",
        ),
        sa.CheckConstraint("version >= 0", name="ck_rebuild_operation_version_nonnegative"),
        sa.ForeignKeyConstraint(
            ["old_generation_id"],
            ["core_generation.id"],
            name="fk_rebuild_operation_old_generation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_generation_id"],
            ["core_generation.id"],
            name="fk_rebuild_operation_target_generation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "target_generation_id", name="uq_rebuild_operation_id_target"),
        sa.UniqueConstraint("target_generation_id", name="uq_rebuild_operation_target_generation"),
    )
    op.create_table(
        "core_generation_revision",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="write_intent", nullable=False),
        sa.Column(
            "write_intent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("parsed_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("parsed_segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cleanup_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("physical_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parsed_text_sha256 IS NULL OR parsed_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_core_generation_revision_parsed_text_sha256",
        ),
        sa.CheckConstraint(
            "state IN ('write_intent', 'indexing', 'failed', 'verified', 'cleaned')",
            name="ck_core_generation_revision_state",
        ),
        sa.CheckConstraint(
            "claim_fence >= 0", name="ck_core_generation_revision_claim_fence_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["core_generation.id"],
            name="fk_core_generation_revision_generation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_core_generation_revision_revision_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("generation_id", "revision_id"),
    )
    op.create_table(
        "core_selector",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("active_generation_id", sa.Uuid(), nullable=True),
        sa.Column("pending_rebuild_id", sa.Uuid(), nullable=True),
        sa.Column("frozen", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("restart_required", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("error", sa.String(length=4000), nullable=True),
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
            "execution_epoch >= 0", name="ck_core_selector_execution_epoch_nonnegative"
        ),
        sa.CheckConstraint("id = 1", name="ck_core_selector_singleton_id"),
        sa.CheckConstraint(
            "pending_rebuild_id IS NULL OR frozen", name="ck_core_selector_pending_frozen"
        ),
        sa.CheckConstraint("version >= 0", name="ck_core_selector_version_nonnegative"),
        sa.ForeignKeyConstraint(
            ["active_generation_id"],
            ["core_generation.id"],
            name="fk_core_selector_active_generation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pending_rebuild_id"],
            ["rebuild_operation.id"],
            name="fk_core_selector_pending_rebuild",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rebuild_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("lifecycle_version", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("vault_path", sa.String(length=2048), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_parsed_text_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "original_parsed_segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("original_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_claim_token", sa.Uuid(), nullable=True),
        sa.Column("parent_claim_fence", sa.BigInteger(), nullable=True),
        sa.Column("cleanup_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parsed_text_sha256", sa.String(length=64), nullable=True),
        sa.Column("parsed_segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("physical_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
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
            "original_parsed_text_sha256 IS NULL OR original_parsed_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rebuild_item_original_parsed_text_sha256",
        ),
        sa.CheckConstraint(
            "parsed_text_sha256 IS NULL OR parsed_text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rebuild_item_parsed_text_sha256",
        ),
        sa.CheckConstraint(
            "snapshot_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rebuild_item_snapshot_sha256"
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rebuild_item_source_sha256"
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'failed', 'verified')", name="ck_rebuild_item_state"
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL AND lease_owner IS NULL AND lease_until IS NULL) OR (claim_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_rebuild_item_lease_group",
        ),
        sa.CheckConstraint(
            "(parent_claim_token IS NULL) = (parent_claim_fence IS NULL)",
            name="ck_rebuild_item_parent_claim_pair",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_rebuild_item_attempts_nonnegative"),
        sa.CheckConstraint("claim_fence >= 0", name="ck_rebuild_item_claim_fence_nonnegative"),
        sa.CheckConstraint(
            "claim_token IS NULL OR parent_claim_token IS NOT NULL",
            name="ck_rebuild_item_claim_parent",
        ),
        sa.CheckConstraint(
            "lifecycle_version >= 0", name="ck_rebuild_item_lifecycle_version_nonnegative"
        ),
        sa.CheckConstraint(
            "parent_claim_fence IS NULL OR parent_claim_fence >= 0",
            name="ck_rebuild_item_parent_claim_fence_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "generation_id"],
            ["rebuild_operation.id", "rebuild_operation.target_generation_id"],
            name="fk_rebuild_item_operation_generation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_id"],
            ["source_revision.id", "source_revision.source_id"],
            name="fk_rebuild_item_revision_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "revision_id", name="uq_rebuild_item_operation_revision"
        ),
        sa.UniqueConstraint("operation_id", "source_id", name="uq_rebuild_item_operation_source"),
    )
    op.add_column("core_maintenance_job", sa.Column("generation_id", sa.Uuid(), nullable=True))
    op.add_column(
        "core_maintenance_job", sa.Column("execution_epoch", sa.BigInteger(), nullable=True)
    )
    op.add_column("core_maintenance_job", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "core_maintenance_job",
        sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "core_maintenance_job", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "core_maintenance_job", sa.Column("retired_reason", sa.String(length=255), nullable=True)
    )
    op.create_foreign_key(
        "fk_core_maintenance_job_core_generation",
        "core_maintenance_job",
        "core_generation",
        ["generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("generation_job", sa.Column("generation_id", sa.Uuid(), nullable=True))
    op.add_column("generation_job", sa.Column("execution_epoch", sa.BigInteger(), nullable=True))
    op.add_column("generation_job", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "generation_job",
        sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "generation_job", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "generation_job", sa.Column("retired_reason", sa.String(length=255), nullable=True)
    )
    op.create_foreign_key(
        "fk_generation_job_core_generation",
        "generation_job",
        "core_generation",
        ["generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("job", sa.Column("generation_id", sa.Uuid(), nullable=True))
    op.add_column("job", sa.Column("execution_epoch", sa.BigInteger(), nullable=True))
    op.add_column("job", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "job", sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("job", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job", sa.Column("retired_reason", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_job_core_generation",
        "job",
        "core_generation",
        ["generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("query_job", sa.Column("generation_id", sa.Uuid(), nullable=True))
    op.add_column("query_job", sa.Column("execution_epoch", sa.BigInteger(), nullable=True))
    op.add_column("query_job", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "query_job", sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("query_job", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("query_job", sa.Column("retired_reason", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_query_job_core_generation",
        "query_job",
        "core_generation",
        ["generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("source_file_operation", sa.Column("generation_id", sa.Uuid(), nullable=True))
    op.add_column(
        "source_file_operation", sa.Column("execution_epoch", sa.BigInteger(), nullable=True)
    )
    op.add_column("source_file_operation", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "source_file_operation",
        sa.Column("claim_fence", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "source_file_operation", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "source_file_operation", sa.Column("retired_reason", sa.String(length=255), nullable=True)
    )
    op.create_foreign_key(
        "fk_source_file_operation_core_generation",
        "source_file_operation",
        "core_generation",
        ["generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("source_revision", sa.Column("indexed_generation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_source_revision_core_generation",
        "source_revision",
        "core_generation",
        ["indexed_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_check_constraint("ck_job_claim_fence_nonnegative", "job", "claim_fence >= 0")
    op.create_check_constraint(
        "ck_job_execution_epoch_nonnegative",
        "job",
        "execution_epoch IS NULL OR execution_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_core_maintenance_claim_fence_nonnegative", "core_maintenance_job", "claim_fence >= 0"
    )
    op.create_check_constraint(
        "ck_core_maintenance_execution_epoch_nonnegative",
        "core_maintenance_job",
        "execution_epoch IS NULL OR execution_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_source_file_operation_claim_fence_nonnegative",
        "source_file_operation",
        "claim_fence >= 0",
    )
    op.create_check_constraint(
        "ck_source_file_operation_execution_epoch_nonnegative",
        "source_file_operation",
        "execution_epoch IS NULL OR execution_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_generation_job_claim_fence_nonnegative", "generation_job", "claim_fence >= 0"
    )
    op.create_check_constraint(
        "ck_generation_job_execution_epoch_nonnegative",
        "generation_job",
        "execution_epoch IS NULL OR execution_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_query_job_claim_fence_nonnegative", "query_job", "claim_fence >= 0"
    )
    op.create_check_constraint(
        "ck_query_job_execution_epoch_nonnegative",
        "query_job",
        "execution_epoch IS NULL OR execution_epoch >= 0",
    )

    op.create_index(
        "ix_rebuild_operation_state_lease_created",
        "rebuild_operation",
        ["state", "lease_until", "created_at"],
    )
    op.create_index(
        "ix_rebuild_item_operation_state_lease_source",
        "rebuild_item",
        ["operation_id", "state", "lease_until", "source_id"],
    )
    op.create_index(
        "ix_core_generation_revision_generation_source_state",
        "core_generation_revision",
        ["generation_id", "source_id", "state"],
    )
    op.create_check_constraint(
        "ck_rebuild_item_verified_metadata",
        "rebuild_item",
        "state != 'verified' OR (verified_at IS NOT NULL AND parsed_text_sha256 IS NOT NULL AND parsed_segments IS NOT NULL AND physical_indexed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_core_generation_revision_verified_metadata",
        "core_generation_revision",
        "state != 'verified' OR (verified_at IS NOT NULL AND parsed_text_sha256 IS NOT NULL AND parsed_segments IS NOT NULL AND physical_indexed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_core_generation_revision_cleaned_metadata",
        "core_generation_revision",
        "state != 'cleaned' OR cleaned_at IS NOT NULL",
    )

    op.create_index(
        "ix_core_generation_revision_revision_source",
        "core_generation_revision",
        ["revision_id", "source_id"],
    )
    op.create_index("ix_rebuild_item_revision_source", "rebuild_item", ["revision_id", "source_id"])
    op.create_index(
        "ix_rebuild_operation_old_generation", "rebuild_operation", ["old_generation_id"]
    )
    op.create_index("ix_core_selector_active_generation", "core_selector", ["active_generation_id"])
    op.create_index("ix_core_selector_pending_rebuild", "core_selector", ["pending_rebuild_id"])
    op.create_index("ix_job_generation", "job", ["generation_id"])
    op.create_index("ix_core_maintenance_generation", "core_maintenance_job", ["generation_id"])
    op.create_index(
        "ix_source_file_operation_generation", "source_file_operation", ["generation_id"]
    )
    op.create_index("ix_generation_job_generation", "generation_job", ["generation_id"])
    op.create_index("ix_query_job_generation", "query_job", ["generation_id"])
    op.create_index(
        "ix_source_revision_indexed_generation", "source_revision", ["indexed_generation_id"]
    )

    op.create_index(
        "ix_rebuild_item_operation_generation", "rebuild_item", ["operation_id", "generation_id"]
    )


def downgrade() -> None:
    # Dropping populated ledgers would erase durable write intents and recovery state.
    connection = op.get_bind()
    # Match the selector/domain lock order and block all writes through the checks
    # and subsequent DDL; an EXISTS check alone is not a downgrade fence.
    locked_tables = (
        "core_selector",
        "rebuild_operation",
        "source_document",
        "job",
        "core_maintenance_job",
        "source_file_operation",
        "generation_job",
        "query_job",
        "source_revision",
        "rebuild_item",
        "core_generation_revision",
        "core_generation",
    )
    for table in locked_tables:
        connection.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    for table in (
        "core_selector",
        "core_generation",
        "rebuild_operation",
        "rebuild_item",
        "core_generation_revision",
    ):
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("Cannot downgrade populated Core generation ledgers")
    for table in (
        "job",
        "core_maintenance_job",
        "source_file_operation",
        "generation_job",
        "query_job",
    ):
        if connection.scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {table} WHERE "
                "generation_id IS NOT NULL OR execution_epoch IS NOT NULL OR "
                "claim_token IS NOT NULL OR claim_fence != 0 OR retired_at IS NOT NULL "
                "OR retired_reason IS NOT NULL)"
            )
        ):
            raise RuntimeError("Cannot downgrade tasks with execution metadata")
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM source_revision WHERE indexed_generation_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("Cannot downgrade generation-bound revisions")
    op.drop_index("ix_rebuild_item_operation_generation", table_name="rebuild_item")
    op.drop_index(
        "ix_core_generation_revision_revision_source", table_name="core_generation_revision"
    )
    op.drop_index("ix_rebuild_item_revision_source", table_name="rebuild_item")
    op.drop_index("ix_rebuild_operation_old_generation", table_name="rebuild_operation")
    op.drop_index("ix_core_selector_active_generation", table_name="core_selector")
    op.drop_index("ix_core_selector_pending_rebuild", table_name="core_selector")
    op.drop_index("ix_job_generation", table_name="job")
    op.drop_index("ix_core_maintenance_generation", table_name="core_maintenance_job")
    op.drop_index("ix_source_file_operation_generation", table_name="source_file_operation")
    op.drop_index("ix_generation_job_generation", table_name="generation_job")
    op.drop_index("ix_query_job_generation", table_name="query_job")
    op.drop_index("ix_source_revision_indexed_generation", table_name="source_revision")
    op.drop_constraint("ck_job_claim_fence_nonnegative", "job", type_="check")
    op.drop_constraint("ck_job_execution_epoch_nonnegative", "job", type_="check")
    op.drop_constraint(
        "ck_core_maintenance_claim_fence_nonnegative", "core_maintenance_job", type_="check"
    )
    op.drop_constraint(
        "ck_core_maintenance_execution_epoch_nonnegative", "core_maintenance_job", type_="check"
    )
    op.drop_constraint(
        "ck_source_file_operation_claim_fence_nonnegative", "source_file_operation", type_="check"
    )
    op.drop_constraint(
        "ck_source_file_operation_execution_epoch_nonnegative",
        "source_file_operation",
        type_="check",
    )
    op.drop_constraint("ck_generation_job_claim_fence_nonnegative", "generation_job", type_="check")
    op.drop_constraint(
        "ck_generation_job_execution_epoch_nonnegative", "generation_job", type_="check"
    )
    op.drop_constraint("ck_query_job_claim_fence_nonnegative", "query_job", type_="check")
    op.drop_constraint("ck_query_job_execution_epoch_nonnegative", "query_job", type_="check")
    # Population was checked under table locks; durable ledger state cannot be discarded.
    op.drop_constraint("fk_source_revision_core_generation", "source_revision", type_="foreignkey")
    op.drop_column("source_revision", "indexed_generation_id")
    op.drop_constraint(
        "fk_source_file_operation_core_generation", "source_file_operation", type_="foreignkey"
    )
    op.drop_column("source_file_operation", "retired_reason")
    op.drop_column("source_file_operation", "retired_at")
    op.drop_column("source_file_operation", "claim_fence")
    op.drop_column("source_file_operation", "claim_token")
    op.drop_column("source_file_operation", "execution_epoch")
    op.drop_column("source_file_operation", "generation_id")
    op.drop_constraint("fk_query_job_core_generation", "query_job", type_="foreignkey")
    op.drop_column("query_job", "retired_reason")
    op.drop_column("query_job", "retired_at")
    op.drop_column("query_job", "claim_fence")
    op.drop_column("query_job", "claim_token")
    op.drop_column("query_job", "execution_epoch")
    op.drop_column("query_job", "generation_id")
    op.drop_constraint("fk_job_core_generation", "job", type_="foreignkey")
    op.drop_column("job", "retired_reason")
    op.drop_column("job", "retired_at")
    op.drop_column("job", "claim_fence")
    op.drop_column("job", "claim_token")
    op.drop_column("job", "execution_epoch")
    op.drop_column("job", "generation_id")
    op.drop_constraint("fk_generation_job_core_generation", "generation_job", type_="foreignkey")
    op.drop_column("generation_job", "retired_reason")
    op.drop_column("generation_job", "retired_at")
    op.drop_column("generation_job", "claim_fence")
    op.drop_column("generation_job", "claim_token")
    op.drop_column("generation_job", "execution_epoch")
    op.drop_column("generation_job", "generation_id")
    op.drop_constraint(
        "fk_core_maintenance_job_core_generation", "core_maintenance_job", type_="foreignkey"
    )
    op.drop_column("core_maintenance_job", "retired_reason")
    op.drop_column("core_maintenance_job", "retired_at")
    op.drop_column("core_maintenance_job", "claim_fence")
    op.drop_column("core_maintenance_job", "claim_token")
    op.drop_column("core_maintenance_job", "execution_epoch")
    op.drop_column("core_maintenance_job", "generation_id")
    op.drop_table("rebuild_item")
    op.drop_table("core_selector")
    op.drop_table("core_generation_revision")
    op.drop_table("rebuild_operation")
    op.drop_table("core_generation")
