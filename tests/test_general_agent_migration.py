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
