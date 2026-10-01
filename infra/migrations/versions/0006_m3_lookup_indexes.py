"""Index provenance paths and generation target foreign keys.

Revision ID: 0006_m3_lookup_indexes
Revises: 0005_m3_generation
"""

from alembic import op


revision = "0006_m3_lookup_indexes"
down_revision = "0005_m3_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_source_revision_vault_path", "source_revision", ["vault_path"])
    op.create_index("ix_generation_job_target_page", "generation_job", ["target_page_id"])
    op.create_index("ix_generated_page_proposal_target", "generated_page", ["proposal_target_page_id"])


def downgrade() -> None:
    op.drop_index("ix_generated_page_proposal_target", table_name="generated_page")
    op.drop_index("ix_generation_job_target_page", table_name="generation_job")
    op.drop_index("ix_source_revision_vault_path", table_name="source_revision")
