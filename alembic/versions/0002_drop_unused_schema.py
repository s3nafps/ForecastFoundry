"""drop unused observations table and events.start_time

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.models
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_observations_station_time", table_name="observations")
    op.drop_table("observations")
    op.drop_column("events", "start_time")


def downgrade() -> None:
    op.add_column("events", sa.Column("start_time", app.models.UTCDateTime(), nullable=True))
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("station_id", sa.String(length=32), nullable=False),
        sa.Column("observed_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("air_temperature", sa.Float(), nullable=True),
        sa.Column("precipitation", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("retrieved_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_observations_station_time", "observations", ["station_id", "observed_at"], unique=False
    )
