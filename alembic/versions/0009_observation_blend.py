"""Add observation blend columns to probability_estimates.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("probability_estimates") as batch_op:
        batch_op.add_column(
            sa.Column("observations_used", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("blend_applied", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("probability_estimates") as batch_op:
        batch_op.drop_column("blend_applied")
        batch_op.drop_column("observations_used")
