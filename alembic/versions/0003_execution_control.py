"""durable execution control and operator credentials

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_control_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_execution_control_state_paused", "execution_control_state", ["paused"])
    op.create_table(
        "operator_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(256), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_operator_credentials_expires_at", "operator_credentials", ["expires_at"])
    with op.batch_alter_table("kill_switch_events") as batch:
        batch.add_column(sa.Column("request_id", sa.String(80), nullable=True))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=True))
    op.create_index("ix_kill_switch_events_request_id", "kill_switch_events", ["request_id"])
    op.execute(
        "INSERT INTO execution_control_state "
        "(id, paused, revision, request_id, actor, reason, updated_at) "
        "VALUES (1, TRUE, 0, 'migration-0003', 'migration', "
        "'startup safety default', CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.drop_index("ix_kill_switch_events_request_id", table_name="kill_switch_events")
    with op.batch_alter_table("kill_switch_events") as batch:
        batch.drop_column("revision")
        batch.drop_column("request_id")
    op.drop_index("ix_operator_credentials_expires_at", table_name="operator_credentials")
    op.drop_table("operator_credentials")
    op.drop_index("ix_execution_control_state_paused", table_name="execution_control_state")
    op.drop_table("execution_control_state")
