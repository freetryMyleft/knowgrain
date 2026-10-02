"""Persist source lifecycle transition versions.

Revision ID: 0010_m5_source_lifecycle
Revises: 0009_m4_entity_lookup
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_m5_source_lifecycle"
down_revision: Union[str, None] = "0009_m4_entity_lookup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column("lifecycle_version", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_source_document_lifecycle_version_nonnegative",
        "source_document",
        "lifecycle_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_source_document_lifecycle_version_nonnegative",
        "source_document",
        type_="check",
    )
    op.drop_column("source_document", "lifecycle_version")
