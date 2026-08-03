"""bind execution control idempotency requests

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_control_requests",
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("target_paused", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("result_paused", sa.Boolean(), nullable=False),
        sa.Column("result_revision", sa.Integer(), nullable=False),
        sa.Column("result_actor", sa.String(120), nullable=False),
        sa.Column("result_reason", sa.Text(), nullable=False),
        sa.Column("result_updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("request_id"),
    )


def downgrade() -> None:
    op.drop_table("execution_control_requests")
