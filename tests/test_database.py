import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from app.database import make_engine, make_session_factory
from app.models import Base, Event, Market, Outcome, RejectedSignal

REQUIRED_TABLES = {
    "events",
    "markets",
    "outcomes",
    "order_book_snapshots",
    "normalized_rules",
    "forecast_runs",
    "forecast_members",
    "observations",
    "probability_estimates",
    "signals",
    "rejected_signals",
    "paper_positions",
    "paper_settlements",
    "provider_errors",
    "application_settings",
}


def test_migration_creates_all_milestone_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
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
    assert REQUIRED_TABLES <= tables


@pytest.mark.asyncio
async def test_market_and_rejection_persist_with_utc_timestamps(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'persistence.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    captured_at = datetime(2026, 8, 2, 9, 30, tzinfo=UTC)

    async with sessions() as session:
        event = Event(
            polymarket_id="775541",
            title="Highest temperature in London on August 2?",
            original_rules="Recorded at London City Airport Station.",
            active=True,
            closed=False,
            raw_data={"id": "775541"},
        )
        session.add(event)
        await session.flush()
        market = Market(
            event_id=event.id,
            polymarket_id="3237363",
            condition_id="0xcondition",
            question="Will the highest temperature be 23°C?",
            active=True,
            closed=False,
            raw_data={"outcomes": '["Yes", "No"]'},
        )
        session.add(market)
        await session.flush()
        session.add(Outcome(market_id=market.id, label="Yes", token_id="yes-token"))
        session.add(
            RejectedSignal(
                market_id=market.id,
                generated_at=captured_at,
                reasons=["spread_above_maximum"],
                candidate_data={"spread": "0.05"},
            )
        )
        await session.commit()

    async with sessions() as session:
        rejection = (await session.scalars(select(RejectedSignal))).one()
        outcome = (await session.scalars(select(Outcome))).one()

    assert rejection.reasons == ["spread_above_maximum"]
    assert rejection.generated_at == captured_at
    assert rejection.generated_at.tzinfo is UTC
    assert outcome.token_id == "yes-token"
    await engine.dispose()
