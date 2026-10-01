"""Persist the singleton application Vault selection.

Revision ID: 0002_m1_vault_binding
Revises: 0001_m1_sources
Create Date: 2026-09-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_m1_vault_binding"
down_revision: Union[str, None] = "0001_m1_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vault_binding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("root_path", sa.String(length=4096), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_vault_binding_singleton_id"),
        sa.PrimaryKeyConstraint("id", name="pk_vault_binding"),
        sa.UniqueConstraint("binding_id", name="uq_vault_binding_binding_id"),
    )


def downgrade() -> None:
    op.drop_table("vault_binding")
