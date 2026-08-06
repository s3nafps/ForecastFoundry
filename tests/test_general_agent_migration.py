import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from app.models import Base
from app.services.contracts import contract_fingerprint

NEW_TABLES = {
    "domain_contracts",
    "provider_registry",
    "evidence_snapshots",
    "prediction_runs",
    "prediction_features",
    "calibration_metrics",
    "research_documents",
    "execution_orders",
    "execution_fills",
    "reconciliation_events",
    "kill_switch_events",
    "keystore_metadata",
    "execution_control_requests",
}


def test_general_agent_migration_creates_auditable_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "general-agent.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert NEW_TABLES <= tables


def test_probability_estimate_observation_columns_exist() -> None:
    # Covered by the fresh upgrade/downgrade test in this module; assert here
    # that the columns are declared on the model.
    from app.models import ProbabilityEstimate

    assert hasattr(ProbabilityEstimate, "observations_used")
    assert hasattr(ProbabilityEstimate, "blend_applied")


def test_models_keep_live_orders_separate_from_paper_positions() -> None:
    assert "execution_orders" in Base.metadata.tables
    assert "paper_positions" in Base.metadata.tables
    assert Base.metadata.tables["execution_orders"].c.mode.default.arg == "paper"
    assert "private_key" not in Base.metadata.tables["keystore_metadata"].columns


def test_execution_control_request_ids_are_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "control-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "SELECT * FROM pragma_table_info('execution_control_requests')"
            ).fetchall()
        }
        primary_key = connection.execute(
            "SELECT pk FROM pragma_table_info('execution_control_requests') "
            "WHERE name = 'request_id'"
        ).fetchone()
    assert primary_key == (1,)
    assert {
        "target_paused",
        "actor",
        "operation",
        "reason",
        "expected_revision",
        "result_revision",
    } <= columns


def test_idempotency_downgrade_preserves_control_and_audit_data(tmp_path: Path) -> None:
    database_path = tmp_path / "control-downgrade.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO kill_switch_events "
            "(active, actor, reason, triggered_at, metadata_json, request_id, revision) "
            "VALUES (1, 'test', 'preserve', CURRENT_TIMESTAMP, '{}', 'preserve-1', 1)"
        )
    command.downgrade(config, "0004")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        state_rows = connection.execute("SELECT COUNT(*) FROM execution_control_state").fetchone()
        audit_rows = connection.execute("SELECT COUNT(*) FROM kill_switch_events").fetchone()
    assert "execution_control_requests" not in tables
    assert state_rows == (1,)
    assert audit_rows == (1,)


def test_contract_identity_migration_consolidates_duplicates_and_preserves_predictions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "contract-identity.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "0005")
    created_at = datetime(2026, 8, 3, 10, tzinfo=UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        for fingerprint, accepted, reasons in (
            ("legacy-accepted", 1, "[]"),
            ("legacy-rejected", 0, '["unsupported_public_source"]'),
        ):
            connection.execute(
                "INSERT INTO domain_contracts "
                "(market_external_id, domain, accepted, resolution_source, expiry, contract_data, "
                "rejection_reasons, provenance, fingerprint, created_at, updated_at) "
                "VALUES (?, 'crypto', ?, 'chainlink', NULL, '{}', ?, '{}', ?, ?, ?)",
                ("btc-duplicate", accepted, reasons, fingerprint, created_at, created_at),
            )
        contract_ids = [
            row[0]
            for row in connection.execute("SELECT id FROM domain_contracts ORDER BY id").fetchall()
        ]
        for index, contract_id in enumerate(contract_ids):
            connection.execute(
                "INSERT INTO prediction_runs "
                "(contract_id, generated_at, model_name, model_version, input_hash, parameters, "
                "probabilities, uncertainty, status) "
                "VALUES (?, ?, 'fixture', 'v1', ?, '{}', '{}', NULL, 'paper_candidate')",
                (contract_id, created_at, f"input-{index}"),
            )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, accepted, rejection_reasons, fingerprint FROM domain_contracts "
            "WHERE market_external_id = 'btc-duplicate' AND domain = 'crypto'"
        ).fetchall()
        prediction_contract_ids = connection.execute(
            "SELECT contract_id FROM prediction_runs ORDER BY id"
        ).fetchall()
    assert rows == [
        (
            max(contract_ids),
            0,
            '["unsupported_public_source"]',
            contract_fingerprint(market_id="btc-duplicate", domain="crypto"),
        )
    ]
    assert prediction_contract_ids == [(max(contract_ids),), (max(contract_ids),)]

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO domain_contracts "
                "(market_external_id, domain, accepted, contract_data, rejection_reasons, "
                "provenance, fingerprint, created_at, updated_at) "
                "VALUES ('btc-duplicate', 'crypto', 0, '{}', '[]', '{}', "
                "'different-fingerprint', ?, ?)",
                (created_at, created_at),
            )

    command.downgrade(config, "0005")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO domain_contracts "
            "(market_external_id, domain, accepted, contract_data, rejection_reasons, provenance, "
            "fingerprint, created_at, updated_at) "
            "VALUES ('btc-duplicate', 'crypto', 0, '{}', '[]', '{}', "
            "'post-downgrade-duplicate', ?, ?)",
            (created_at, created_at),
        )
        count = connection.execute(
            "SELECT COUNT(*) FROM domain_contracts "
            "WHERE market_external_id = 'btc-duplicate' AND domain = 'crypto'"
        ).fetchone()
        prediction_contract_ids = connection.execute(
            "SELECT contract_id FROM prediction_runs ORDER BY id"
        ).fetchall()
    assert count == (2,)
    assert prediction_contract_ids == [(max(contract_ids),), (max(contract_ids),)]
