"""Use a 64-bit identity for rebuildable Wiki links without resetting data.

Revision ID: 0004_m2_link_identity
Revises: 0003_m2_wiki
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_m2_link_identity"
down_revision = "0003_m2_wiki"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("page_link", "id", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False)
    op.execute("ALTER SEQUENCE page_link_id_seq AS BIGINT")


def downgrade() -> None:
    # PostgreSQL refuses narrowing if existing values/sequence exceed int32.
    # Never reset the sequence or delete rows to make a downgrade fit.
    op.execute("ALTER SEQUENCE page_link_id_seq AS INTEGER")
    op.alter_column("page_link", "id", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False)
