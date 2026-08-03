"""persist provider health probes

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_health_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(160), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_health_name_checked",
        "provider_health_snapshots",
        ["provider", "checked_at"],
    )
    op.create_index("ix_provider_health_snapshots_provider", "provider_health_snapshots", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_provider_health_snapshots_provider", table_name="provider_health_snapshots")
    op.drop_index("ix_provider_health_name_checked", table_name="provider_health_snapshots")
    op.drop_table("provider_health_snapshots")
