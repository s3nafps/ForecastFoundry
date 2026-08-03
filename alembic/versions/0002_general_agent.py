"""general agent audit and execution tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03
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
    op.create_table(
        "domain_contracts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("market_external_id", sa.String(100), nullable=False),
        sa.Column("domain", sa.String(40), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("resolution_source", sa.Text(), nullable=True),
        sa.Column("expiry", app.models.UTCDateTime(), nullable=True),
        sa.Column("contract_data", sa.JSON(), nullable=False),
        sa.Column("rejection_reasons", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("created_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_domain_contracts_domain", "domain_contracts", ["domain"], unique=False)
    op.create_index(
        "ix_domain_contracts_domain_expiry",
        "domain_contracts",
        ["domain", "expiry"],
        unique=False,
    )
    op.create_index("ix_domain_contracts_expiry", "domain_contracts", ["expiry"], unique=False)
    op.create_index(
        "ix_domain_contracts_fingerprint", "domain_contracts", ["fingerprint"], unique=True
    )
    op.create_index(
        "ix_domain_contracts_market_external_id",
        "domain_contracts",
        ["market_external_id"],
        unique=False,
    )

    op.create_table(
        "provider_registry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("domain", sa.String(40), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("auth_mode", sa.String(24), nullable=False),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "evidence_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("provider_version", sa.String(80), nullable=True),
        sa.Column("source_timestamp", app.models.UTCDateTime(), nullable=True),
        sa.Column("retrieved_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("raw_response_hash", sa.String(128), nullable=False),
        sa.Column("normalized_values", sa.JSON(), nullable=False),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("freshness_seconds", sa.Integer(), nullable=True),
        sa.Column("license_metadata", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_provider_source_time",
        "evidence_snapshots",
        ["provider", "source_timestamp"],
        unique=False,
    )

    op.create_table(
        "prediction_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("generated_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("model_version", sa.String(80), nullable=False),
        sa.Column("input_hash", sa.String(128), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("probabilities", sa.JSON(), nullable=False),
        sa.Column("uncertainty", sa.String(40), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["contract_id"], ["domain_contracts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prediction_runs_market_generated",
        "prediction_runs",
        ["market_id", "generated_at"],
        unique=False,
    )

    op.create_table(
        "prediction_features",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prediction_run_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("live_eligible", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["prediction_run_id"], ["prediction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prediction_features_prediction_run_id",
        "prediction_features",
        ["prediction_run_id"],
        unique=False,
    )

    op.create_table(
        "calibration_metrics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prediction_run_id", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("window_start", app.models.UTCDateTime(), nullable=False),
        sa.Column("window_end", app.models.UTCDateTime(), nullable=False),
        sa.Column("brier_score", sa.Numeric(18, 8), nullable=False),
        sa.Column("reliability_buckets", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["prediction_run_id"], ["prediction_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "research_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", app.models.UTCDateTime(), nullable=True),
        sa.Column("retrieved_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("redacted_text", sa.Text(), nullable=False),
        sa.Column("feature_only", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_provider_published",
        "research_documents",
        ["provider", "published_at"],
        unique=False,
    )

    op.create_table(
        "execution_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("outcome_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("client_order_id", sa.String(120), nullable=False),
        sa.Column("provider_order_id", sa.String(160), nullable=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("size", sa.Numeric(18, 8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("live_authorized", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", app.models.UTCDateTime(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcomes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint("provider_order_id"),
    )
    op.create_index("ix_execution_orders_mode", "execution_orders", ["mode"], unique=False)
    op.create_index("ix_execution_orders_status", "execution_orders", ["status"], unique=False)
    op.create_index(
        "ix_execution_orders_status_created",
        "execution_orders",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "execution_fills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_order_id", sa.Integer(), nullable=False),
        sa.Column("provider_fill_id", sa.String(160), nullable=False),
        sa.Column("filled_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("size", sa.Numeric(18, 8), nullable=False),
        sa.Column("fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_order_id"], ["execution_orders.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_fill_id"),
    )
    op.create_index(
        "ix_execution_fills_order_filled",
        "execution_fills",
        ["execution_order_id", "filled_at"],
        unique=False,
    )

    op.create_table(
        "reconciliation_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_order_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expected", sa.JSON(), nullable=False),
        sa.Column("actual", sa.JSON(), nullable=False),
        sa.Column("occurred_at", app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_order_id"], ["execution_orders.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "kill_switch_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("triggered_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kill_switch_events_active", "kill_switch_events", ["active"], unique=False)

    op.create_table(
        "keystore_metadata",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("last_verified_at", app.models.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
        sa.UniqueConstraint("fingerprint"),
    )


def downgrade() -> None:
    op.drop_table("keystore_metadata")
    op.drop_index("ix_kill_switch_events_active", table_name="kill_switch_events")
    op.drop_table("kill_switch_events")
    op.drop_table("reconciliation_events")
    op.drop_index("ix_execution_fills_order_filled", table_name="execution_fills")
    op.drop_table("execution_fills")
    op.drop_index("ix_execution_orders_status_created", table_name="execution_orders")
    op.drop_index("ix_execution_orders_status", table_name="execution_orders")
    op.drop_index("ix_execution_orders_mode", table_name="execution_orders")
    op.drop_table("execution_orders")
    op.drop_index("ix_research_provider_published", table_name="research_documents")
    op.drop_table("research_documents")
    op.drop_table("calibration_metrics")
    op.drop_index("ix_prediction_features_prediction_run_id", table_name="prediction_features")
    op.drop_table("prediction_features")
    op.drop_index("ix_prediction_runs_market_generated", table_name="prediction_runs")
    op.drop_table("prediction_runs")
    op.drop_index("ix_evidence_provider_source_time", table_name="evidence_snapshots")
    op.drop_table("evidence_snapshots")
    op.drop_table("provider_registry")
    op.drop_index("ix_domain_contracts_fingerprint", table_name="domain_contracts")
    op.drop_index("ix_domain_contracts_market_external_id", table_name="domain_contracts")
    op.drop_index("ix_domain_contracts_expiry", table_name="domain_contracts")
    op.drop_index("ix_domain_contracts_domain_expiry", table_name="domain_contracts")
    op.drop_index("ix_domain_contracts_domain", table_name="domain_contracts")
    op.drop_table("domain_contracts")
