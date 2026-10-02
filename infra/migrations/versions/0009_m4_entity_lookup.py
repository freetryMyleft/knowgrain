"""Index exact chunk membership for Wiki/entity reverse navigation.

Revision ID: 0009_m4_entity_lookup
Revises: 0008_m4_queries
"""

from alembic import op

revision = "0009_m4_entity_lookup"
down_revision = "0008_m4_queries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_evidence_ref_chunk", "evidence_ref", ["chunk_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_ref_chunk", table_name="evidence_ref")
