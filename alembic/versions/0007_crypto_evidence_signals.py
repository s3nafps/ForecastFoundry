"""link crypto evidence, predictions, and signals

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evidence_snapshots") as batch_op:
        batch_op.add_column(sa.Column("contract_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("fingerprint", sa.String(128), nullable=True))
        batch_op.create_foreign_key(
            "fk_evidence_snapshots_contract_id_domain_contracts",
            "domain_contracts",
            ["contract_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint("uq_evidence_snapshots_fingerprint", ["fingerprint"])
    op.create_index("ix_evidence_snapshots_contract_id", "evidence_snapshots", ["contract_id"])
    op.create_index(
        "ix_evidence_snapshots_fingerprint",
        "evidence_snapshots",
        ["fingerprint"],
        unique=True,
    )

    with op.batch_alter_table("prediction_runs") as batch_op:
        batch_op.add_column(sa.Column("evidence_snapshot_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_prediction_runs_evidence_snapshot_id_evidence_snapshots",
            "evidence_snapshots",
            ["evidence_snapshot_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_prediction_runs_evidence_snapshot_id",
        "prediction_runs",
        ["evidence_snapshot_id"],
    )

    with op.batch_alter_table("signals") as batch_op:
        batch_op.alter_column("market_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("outcome_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("contract_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("prediction_run_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("evidence_snapshot_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("side", sa.String(8), nullable=True))
        batch_op.add_column(sa.Column("outcome_label", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("token_id", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("freshness_seconds", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_signals_contract_id_domain_contracts",
            "domain_contracts",
            ["contract_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_signals_prediction_run_id_prediction_runs",
            "prediction_runs",
            ["prediction_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_signals_evidence_snapshot_id_evidence_snapshots",
            "evidence_snapshots",
            ["evidence_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_signals_model_probability",
            "model_probability >= 0 AND model_probability <= 1",
        )
        batch_op.create_check_constraint(
            "ck_signals_executable_ask", "executable_ask >= 0 AND executable_ask <= 1"
        )
        batch_op.create_check_constraint("ck_signals_side", "side IS NULL OR side = 'buy'")
    for column in ("contract_id", "prediction_run_id", "evidence_snapshot_id"):
        op.create_index(f"ix_signals_{column}", "signals", [column])

    with op.batch_alter_table("rejected_signals") as batch_op:
        batch_op.add_column(sa.Column("contract_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("prediction_run_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("evidence_snapshot_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("fingerprint", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("side", sa.String(8), nullable=True))
        batch_op.add_column(sa.Column("outcome_label", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("token_id", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("model_probability", sa.Numeric(18, 8), nullable=True))
        batch_op.add_column(sa.Column("executable_ask", sa.Numeric(18, 8), nullable=True))
        batch_op.add_column(sa.Column("raw_edge", sa.Numeric(18, 8), nullable=True))
        batch_op.add_column(sa.Column("usable_edge", sa.Numeric(18, 8), nullable=True))
        batch_op.add_column(sa.Column("buffers", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("freshness_seconds", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_rejected_signals_contract_id_domain_contracts",
            "domain_contracts",
            ["contract_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_rejected_signals_prediction_run_id_prediction_runs",
            "prediction_runs",
            ["prediction_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_rejected_signals_evidence_snapshot_id_evidence_snapshots",
            "evidence_snapshots",
            ["evidence_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_rejected_signals_fingerprint", ["fingerprint"])
        batch_op.create_check_constraint(
            "ck_rejected_signals_model_probability",
            "model_probability IS NULL OR (model_probability >= 0 AND model_probability <= 1)",
        )
        batch_op.create_check_constraint(
            "ck_rejected_signals_executable_ask",
            "executable_ask IS NULL OR (executable_ask >= 0 AND executable_ask <= 1)",
        )
        batch_op.create_check_constraint("ck_rejected_signals_side", "side IS NULL OR side = 'buy'")
    for column in ("contract_id", "prediction_run_id", "evidence_snapshot_id"):
        op.create_index(f"ix_rejected_signals_{column}", "rejected_signals", [column])
    op.create_index(
        "ix_rejected_signals_fingerprint",
        "rejected_signals",
        ["fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    for column in ("fingerprint", "evidence_snapshot_id", "prediction_run_id", "contract_id"):
        op.drop_index(f"ix_rejected_signals_{column}", table_name="rejected_signals")
    with op.batch_alter_table("rejected_signals") as batch_op:
        batch_op.drop_constraint("ck_rejected_signals_side", type_="check")
        batch_op.drop_constraint("ck_rejected_signals_executable_ask", type_="check")
        batch_op.drop_constraint("ck_rejected_signals_model_probability", type_="check")
        batch_op.drop_constraint("uq_rejected_signals_fingerprint", type_="unique")
        for column in (
            "freshness_seconds",
            "buffers",
            "usable_edge",
            "raw_edge",
            "executable_ask",
            "model_probability",
            "token_id",
            "outcome_label",
            "side",
            "fingerprint",
            "evidence_snapshot_id",
            "prediction_run_id",
            "contract_id",
        ):
            batch_op.drop_column(column)

    for column in ("evidence_snapshot_id", "prediction_run_id", "contract_id"):
        op.drop_index(f"ix_signals_{column}", table_name="signals")
    with op.batch_alter_table("signals") as batch_op:
        batch_op.drop_constraint("ck_signals_side", type_="check")
        batch_op.drop_constraint("ck_signals_executable_ask", type_="check")
        batch_op.drop_constraint("ck_signals_model_probability", type_="check")
        for column in (
            "freshness_seconds",
            "token_id",
            "outcome_label",
            "side",
            "evidence_snapshot_id",
            "prediction_run_id",
            "contract_id",
        ):
            batch_op.drop_column(column)
        batch_op.alter_column("outcome_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("market_id", existing_type=sa.Integer(), nullable=False)

    op.drop_index("ix_prediction_runs_evidence_snapshot_id", table_name="prediction_runs")
    with op.batch_alter_table("prediction_runs") as batch_op:
        batch_op.drop_column("evidence_snapshot_id")

    op.drop_index("ix_evidence_snapshots_fingerprint", table_name="evidence_snapshots")
    op.drop_index("ix_evidence_snapshots_contract_id", table_name="evidence_snapshots")
    with op.batch_alter_table("evidence_snapshots") as batch_op:
        batch_op.drop_constraint("uq_evidence_snapshots_fingerprint", type_="unique")
        batch_op.drop_column("fingerprint")
        batch_op.drop_column("contract_id")
