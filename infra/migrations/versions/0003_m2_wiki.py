"""Add the rebuildable Wiki page and link projection.

Revision ID: 0003_m2_wiki
Revises: 0002_m1_vault_binding
Create Date: 2026-10-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_m2_wiki"
down_revision: Union[str, None] = "0002_m1_vault_binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wiki_page",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("vault_path", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("present", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('draft', 'reviewed')", name="ck_wiki_page_status"),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_wiki_page_sha256_length"),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_wiki_page_sha256_hex"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wiki_page_present", "wiki_page", ["present"])

    op.create_table(
        "page_link",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("to_page_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("embed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_page_id"],
            ["wiki_page.id"],
            name="fk_page_link_from_page_id_wiki_page",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_page_id"],
            ["wiki_page.id"],
            name="fk_page_link_to_page_id_wiki_page",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_page_link_to_page", "page_link", ["to_page_id", "from_page_id"])
    op.create_index("ix_page_link_from_page", "page_link", ["from_page_id"])


def downgrade() -> None:
    op.drop_index("ix_page_link_from_page", table_name="page_link")
    op.drop_index("ix_page_link_to_page", table_name="page_link")
    op.drop_table("page_link")
    op.drop_index("ix_wiki_page_present", table_name="wiki_page")
    op.drop_table("wiki_page")
