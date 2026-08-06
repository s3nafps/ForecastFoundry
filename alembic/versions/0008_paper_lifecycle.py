"""complete idempotent paper lifecycle

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.models
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("execution_orders") as batch_op:
        batch_op.add_column(sa.Column("signal_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("intent_fingerprint", sa.String(128), nullable=True))
        batch_op.create_foreign_key(
            "fk_execution_orders_signal_id_signals",
            "signals",
            ["signal_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint("uq_execution_orders_signal_id", ["signal_id"])
        batch_op.create_unique_constraint(
            "uq_execution_orders_intent_fingerprint", ["intent_fingerprint"]
        )
        batch_op.create_check_constraint("ck_execution_order_price", "price > 0 AND price <= 1")
        batch_op.create_check_constraint("ck_execution_order_size", "size > 0")

    with op.batch_alter_table("execution_fills") as batch_op:
        batch_op.create_check_constraint("ck_execution_fill_price", "price > 0 AND price <= 1")
        batch_op.create_check_constraint("ck_execution_fill_size", "size > 0")
        batch_op.create_check_constraint("ck_execution_fill_fee", "fee >= 0")

    with op.batch_alter_table("paper_positions") as batch_op:
        batch_op.alter_column("market_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("outcome_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("execution_order_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("current_mark", sa.Numeric(18, 8), nullable=True))
        batch_op.add_column(
            sa.Column("unrealized_pnl", sa.Numeric(18, 8), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("realized_pnl", sa.Numeric(18, 8), nullable=False, server_default="0")
        )
        batch_op.create_foreign_key(
            "fk_paper_positions_execution_order_id_execution_orders",
            "execution_orders",
            ["execution_order_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_paper_positions_execution_order_id", ["execution_order_id"]
        )
        batch_op.create_check_constraint(
            "ck_paper_position_price", "entry_price > 0 AND entry_price <= 1"
        )
        batch_op.create_check_constraint("ck_paper_position_shares", "shares > 0")
        batch_op.create_check_constraint("ck_paper_position_amount", "amount > 0")
        batch_op.create_check_constraint("ck_paper_position_fees", "fees >= 0")

    with op.batch_alter_table("paper_settlements") as batch_op:
        batch_op.add_column(sa.Column("evidence_snapshot_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("outcome_label", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("request_id", sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("actor", sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("request_fingerprint", sa.String(128), nullable=True))
        batch_op.create_foreign_key(
            "fk_paper_settlements_evidence_snapshot_id_evidence_snapshots",
            "evidence_snapshots",
            ["evidence_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_paper_settlements_request_id", ["request_id"])
        batch_op.create_check_constraint("ck_paper_settlement_payout", "payout >= 0")

    with op.batch_alter_table("calibration_metrics") as batch_op:
        batch_op.add_column(sa.Column("settlement_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_calibration_metrics_settlement_id_paper_settlements",
            "paper_settlements",
            ["settlement_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint("uq_calibration_metrics_settlement_id", ["settlement_id"])

    op.create_table(
        "paper_execution_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(120), nullable=True),
        sa.Column("actor", sa.String(120), nullable=True),
        sa.Column("request_fingerprint", sa.String(128), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("requested_shares", sa.Numeric(18, 8), nullable=False),
        sa.Column("approved_shares", sa.Numeric(18, 8), nullable=False),
        sa.Column("balance_before", sa.Numeric(18, 8), nullable=False),
        sa.Column("exposure_before", sa.Numeric(18, 8), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(18, 8), nullable=False),
        sa.Column("created_at", app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signal_id"),
        sa.UniqueConstraint("request_id"),
    )


def downgrade() -> None:
    # Refuse before the first DDL statement when 0007 cannot represent populated
    # lifecycle state. A downgrade must never silently turn financial history into
    # data loss; operators can back up and perform an explicit forward recovery.
    connection = op.get_bind()
    incompatible_checks = {
        "unrepresentable positions": (
            "SELECT COUNT(*) FROM paper_positions WHERE market_id IS NULL OR outcome_id IS NULL"
        ),
        "execution decisions": "SELECT COUNT(*) FROM paper_execution_decisions",
        "lifecycle orders": (
            "SELECT COUNT(*) FROM execution_orders "
            "WHERE signal_id IS NOT NULL OR intent_fingerprint IS NOT NULL"
        ),
        "authoritative settlements": (
            "SELECT COUNT(*) FROM paper_settlements WHERE evidence_snapshot_id IS NOT NULL "
            "OR outcome_label IS NOT NULL OR request_id IS NOT NULL OR actor IS NOT NULL "
            "OR request_fingerprint IS NOT NULL"
        ),
        "settlement calibration": (
            "SELECT COUNT(*) FROM calibration_metrics WHERE settlement_id IS NOT NULL"
        ),
    }
    populated = [
        name
        for name, query in incompatible_checks.items()
        if int(connection.execute(sa.text(query)).scalar_one()) > 0
    ]
    if populated:
        raise RuntimeError(
            "0008 downgrade refused before schema changes: populated new lifecycle data "
            f"cannot be represented by 0007 ({', '.join(populated)}). "
            "Back up the database and use forward recovery."
        )
    op.drop_table("paper_execution_decisions")
    with op.batch_alter_table("calibration_metrics") as batch_op:
        batch_op.drop_constraint("uq_calibration_metrics_settlement_id", type_="unique")
        batch_op.drop_column("settlement_id")
    with op.batch_alter_table("paper_settlements") as batch_op:
        batch_op.drop_constraint("ck_paper_settlement_payout", type_="check")
        batch_op.drop_constraint("uq_paper_settlements_request_id", type_="unique")
        batch_op.drop_column("request_fingerprint")
        batch_op.drop_column("actor")
        batch_op.drop_column("request_id")
        batch_op.drop_column("outcome_label")
        batch_op.drop_column("evidence_snapshot_id")
    with op.batch_alter_table("paper_positions") as batch_op:
        for name in (
            "ck_paper_position_fees",
            "ck_paper_position_amount",
            "ck_paper_position_shares",
            "ck_paper_position_price",
        ):
            batch_op.drop_constraint(name, type_="check")
        batch_op.drop_constraint("uq_paper_positions_execution_order_id", type_="unique")
        for column in (
            "realized_pnl",
            "unrealized_pnl",
            "current_mark",
            "execution_order_id",
        ):
            batch_op.drop_column(column)
        batch_op.alter_column("outcome_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("market_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("execution_fills") as batch_op:
        for name in (
            "ck_execution_fill_fee",
            "ck_execution_fill_size",
            "ck_execution_fill_price",
        ):
            batch_op.drop_constraint(name, type_="check")
    with op.batch_alter_table("execution_orders") as batch_op:
        batch_op.drop_constraint("ck_execution_order_size", type_="check")
        batch_op.drop_constraint("ck_execution_order_price", type_="check")
        batch_op.drop_constraint("uq_execution_orders_intent_fingerprint", type_="unique")
        batch_op.drop_constraint("uq_execution_orders_signal_id", type_="unique")
        batch_op.drop_column("intent_fingerprint")
        batch_op.drop_column("signal_id")
