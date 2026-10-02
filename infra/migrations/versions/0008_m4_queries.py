"""Add durable M4 question jobs and immutable result storage.

Revision ID: 0008_m4_queries
Revises: 0007_m3_review
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0008_m4_queries"
down_revision: Union[str, None] = "0007_m3_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "query_job",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("question", sa.String(length=1000), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(length=4000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "length(question) BETWEEN 1 AND 1000 AND length(trim(question)) > 0",
            name="ck_query_job_question_length",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_query_job_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_query_job_attempts_nonnegative"),
        sa.CheckConstraint(
            "(state = 'succeeded') = (result IS NOT NULL)",
            name="ck_query_job_result_state",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_query_job"),
    )
    op.create_index(
        "ix_query_job_state_lease_created",
        "query_job",
        ["state", "lease_until", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_query_job_state_lease_created", table_name="query_job")
    op.drop_table("query_job")
