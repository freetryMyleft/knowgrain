"""Add durable explicit Wiki review operations and bindings.

Revision ID: 0007_m3_review
Revises: 0006_m3_lookup_indexes
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_m3_review"
down_revision: Union[str, None] = "0006_m3_lookup_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_operation",
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation_page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("expected_page_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_generation_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="prepared", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(expected_page_sha256) = 64 AND expected_page_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_operation_expected_page_sha256",
        ),
        sa.CheckConstraint(
            "length(expected_generation_sha256) = 64 AND expected_generation_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_operation_expected_generation_sha256",
        ),
        sa.CheckConstraint(
            "length(reviewed_sha256) = 64 AND reviewed_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_operation_reviewed_sha256",
        ),
        sa.CheckConstraint(
            "state IN ('prepared', 'completed')", name="ck_review_operation_state"
        ),
        sa.CheckConstraint(
            "(state = 'completed') = (completed_at IS NOT NULL)",
            name="ck_review_operation_completed_pair",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["wiki_page.id"],
            name="fk_review_operation_page_id_wiki_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_page_id"],
            ["generated_page.page_id"],
            name="fk_review_operation_generation_page_id_generated_page",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_review_operation"),
    )
    op.create_index("ix_review_operation_page", "review_operation", ["page_id"])
    op.create_index(
        "ix_review_operation_generation", "review_operation", ["generation_page_id"]
    )

    op.create_table(
        "page_generation_binding",
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation_page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reviewed_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "length(reviewed_sha256) = 64 AND reviewed_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_page_generation_binding_reviewed_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["wiki_page.id"],
            name="fk_page_generation_binding_page_id_wiki_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_page_id"],
            ["generated_page.page_id"],
            name="fk_page_generation_binding_generation_page_id_generated_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["review_operation.operation_id"],
            name="fk_page_generation_binding_operation_id_review_operation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("page_id", name="pk_page_generation_binding"),
    )
    op.create_index(
        "ix_page_generation_binding_generation",
        "page_generation_binding",
        ["generation_page_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_page_generation_binding_generation", table_name="page_generation_binding"
    )
    op.drop_table("page_generation_binding")
    op.drop_index("ix_review_operation_generation", table_name="review_operation")
    op.drop_index("ix_review_operation_page", table_name="review_operation")
    op.drop_table("review_operation")
