import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.models import Base

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
