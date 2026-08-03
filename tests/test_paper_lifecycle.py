import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import func, select

from alembic import command
from app.cli import _execute as execute_cli
from app.config import Settings
from app.database import make_engine, make_session_factory
from app.main import create_app
from app.mcp_server import MCPFacade
from app.models import (
    Base,
    CalibrationMetric,
    DomainContract,
    EvidenceSnapshot,
    ExecutionFill,
    ExecutionOrder,
    PaperPosition,
    PaperSettlement,
    PredictionRun,
    Signal,
)
from app.services.application import ApplicationServices
from app.services.paper import PaperLifecycle, SettlementEvidence, SettlementWorker


async def _seed(tmp_path: Path, *, balance: Decimal = Decimal("100")):
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'lifecycle.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    expiry = datetime(2026, 8, 4, tzinfo=UTC)
    async with sessions() as session:
        contract = DomainContract(
            market_external_id="btc-market",
            domain="crypto",
            accepted=True,
            resolution_source="coinbase",
            expiry=expiry,
            contract_data={"asset": "BTC", "outcomes": ["YES", "NO"]},
            rejection_reasons=[],
            provenance={},
            fingerprint="contract-fp",
        )
        session.add(contract)
        await session.flush()
        evidence = EvidenceSnapshot(
            contract_id=contract.id,
            fingerprint="prediction-evidence",
            provider="coinbase",
            provider_version="v1",
            source_timestamp=expiry - timedelta(hours=1),
            retrieved_at=expiry - timedelta(hours=1),
            raw_response_hash="raw",
            normalized_values={"price": "100"},
            quality_flags=[],
            freshness_seconds=0,
            license_metadata={},
        )
        session.add(evidence)
        await session.flush()
        prediction = PredictionRun(
            contract_id=contract.id,
            evidence_snapshot_id=evidence.id,
            generated_at=expiry - timedelta(hours=1),
            model_name="baseline",
            model_version="v1",
            input_hash="input",
            parameters={},
            probabilities={"YES": "0.70", "NO": "0.30"},
            uncertainty="0.02",
            status="paper_candidate",
        )
        session.add(prediction)
        await session.flush()
        signal = Signal(
            contract_id=contract.id,
            prediction_run_id=prediction.id,
            evidence_snapshot_id=evidence.id,
            side="buy",
            outcome_label="YES",
            token_id="yes-token",
            generated_at=expiry - timedelta(hours=1),
            model_probability=Decimal("0.70"),
            executable_ask=Decimal("0.40"),
            raw_edge=Decimal("0.30"),
            usable_edge=Decimal("0.20"),
            buffers={"estimated_fee": "0.01", "slippage": "0.01"},
            fingerprint="signal-fp",
            freshness_seconds=0,
            signal_data={"candidate": {"required_size": "5"}},
        )
        session.add(signal)
        await session.commit()
    return engine, sessions, signal.id, contract.id, expiry


@pytest.mark.asyncio
async def test_signal_executes_and_settles_once_with_calibration(tmp_path: Path) -> None:
    engine, sessions, signal_id, contract_id, expiry = await _seed(tmp_path)
    lifecycle = PaperLifecycle(
        sessions,
        Settings(
            app_env="test",
            scheduler_enabled=False,
            paper_starting_balance=Decimal("100"),
            estimated_fee=Decimal("0.01"),
            slippage_buffer=Decimal("0.01"),
        ),
    )

    first = await lifecycle.execute_signal(signal_id)
    retry = await lifecycle.execute_signal(signal_id)
    assert first == retry
    assert first["status"] == "filled"

    evidence = SettlementEvidence(
        contract_id=contract_id,
        source="coinbase",
        observed_at=expiry,
        retrieved_at=expiry + timedelta(minutes=1),
        outcome_label="YES",
        raw_response_hash="settlement-raw",
        normalized_values={"close": "101"},
        provider_version="v1",
        license_metadata={},
    )
    settled = await lifecycle.settle_position(first["position_id"], evidence)
    database_url = str(engine.url)
    await engine.dispose()
    restarted_engine = make_engine(database_url)
    restarted_sessions = make_session_factory(restarted_engine)
    replay = await PaperLifecycle(
        restarted_sessions,
        Settings(
            app_env="test",
            scheduler_enabled=False,
            paper_starting_balance=Decimal("100"),
            estimated_fee=Decimal("0.01"),
            slippage_buffer=Decimal("0.01"),
        ),
    ).settle_position(first["position_id"], evidence)
    assert settled == replay
    assert settled["won"] is True

    async with restarted_sessions() as session:
        for model in (
            ExecutionOrder,
            ExecutionFill,
            PaperPosition,
            PaperSettlement,
            CalibrationMetric,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        position = await session.get(PaperPosition, first["position_id"])
        assert position is not None and position.realized_pnl > 0
    await restarted_engine.dispose()


@pytest.mark.asyncio
async def test_risk_denial_is_persisted_without_financial_mutation(tmp_path: Path) -> None:
    engine, sessions, signal_id, _, _ = await _seed(tmp_path, balance=Decimal("5"))
    settings = Settings(
        app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("5")
    )
    result = await PaperLifecycle(sessions, settings).execute_signal(signal_id)
    retry = await PaperLifecycle(sessions, settings).execute_signal(signal_id)
    assert result == retry
    assert result["status"] == "rejected"
    assert result["reasons"] == ["risk_cap_below_minimum"]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionOrder)) == 0
        assert await session.scalar(select(func.count()).select_from(PaperPosition)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_settlement_rejects_wrong_authoritative_source(tmp_path: Path) -> None:
    engine, sessions, signal_id, contract_id, expiry = await _seed(tmp_path)
    settings = Settings(
        app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")
    )
    lifecycle = PaperLifecycle(sessions, settings)
    execution = await lifecycle.execute_signal(signal_id)
    evidence = SettlementEvidence(
        contract_id=contract_id,
        source="binance",
        observed_at=expiry,
        retrieved_at=expiry,
        outcome_label="YES",
        raw_response_hash="wrong",
        normalized_values={},
    )
    with pytest.raises(ValueError, match="source"):
        await lifecycle.settle_position(execution["position_id"], evidence)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperSettlement)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_settlement_worker_is_due_only_and_isolates_failures(tmp_path: Path) -> None:
    engine, sessions, signal_id, contract_id, expiry = await _seed(tmp_path)
    settings = Settings(
        app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")
    )
    lifecycle = PaperLifecycle(sessions, settings)
    execution = await lifecycle.execute_signal(signal_id)
    fetched: list[int] = []

    async def fetch(position_id: int) -> SettlementEvidence:
        fetched.append(position_id)
        return SettlementEvidence(
            contract_id=contract_id,
            source="coinbase",
            observed_at=expiry,
            retrieved_at=expiry,
            outcome_label="NO",
            raw_response_hash="worker-resolution",
            normalized_values={"close": "80"},
        )

    assert await SettlementWorker(lifecycle).run_due(fetch, now=expiry - timedelta(seconds=1)) == []
    result = await SettlementWorker(lifecycle).run_due(fetch, now=expiry)
    assert fetched == [execution["position_id"]]
    assert result[0]["status"] == "settled"
    assert result[0]["won"] is False
    assert await SettlementWorker(lifecycle).run_due(fetch, now=expiry) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_position_calibrates_against_the_purchased_outcome(tmp_path: Path) -> None:
    engine, sessions, signal_id, contract_id, expiry = await _seed(tmp_path)
    async with sessions() as session:
        signal = await session.get(Signal, signal_id)
        assert signal is not None
        signal.outcome_label = "NO"
        signal.model_probability = Decimal("0.80")
        await session.commit()
    settings = Settings(
        app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")
    )
    lifecycle = PaperLifecycle(sessions, settings)
    execution = await lifecycle.execute_signal(signal_id)
    await lifecycle.settle_position(
        execution["position_id"],
        SettlementEvidence(
            contract_id=contract_id,
            source="coinbase",
            observed_at=expiry,
            retrieved_at=expiry,
            outcome_label="NO",
            raw_response_hash="no-resolution",
            normalized_values={},
        ),
    )
    async with sessions() as session:
        metric = await session.scalar(select(CalibrationMetric))
        assert metric is not None
        assert metric.brier_score == Decimal("0.04")
    await engine.dispose()


def test_paper_lifecycle_migration_round_trip_preserves_existing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "paper-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "0007")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO application_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("paper_balance", '"17.50"', datetime.now(UTC).isoformat()),
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_positions)")}
        assert {"execution_order_id", "unrealized_pnl", "realized_pnl"} <= columns
        assert connection.execute(
            "SELECT value FROM application_settings WHERE key = 'paper_balance'"
        ).fetchone() == ('"17.50"',)
    command.downgrade(config, "0007")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT value FROM application_settings WHERE key = 'paper_balance'"
        ).fetchone() == ('"17.50"',)


@pytest.mark.asyncio
async def test_cli_rest_and_mcp_share_authoritative_portfolio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sessions, signal_id, _, _ = await _seed(tmp_path)
    database_url = str(engine.url)
    settings = Settings(
        app_env="test",
        scheduler_enabled=False,
        database_url=database_url,
        paper_starting_balance=Decimal("100"),
    )
    await PaperLifecycle(sessions, settings).execute_signal(signal_id)
    services = ApplicationServices(sessions, settings)
    direct = await services.portfolio_status()
    mcp = await MCPFacade(services).portfolio_status()
    monkeypatch.setenv("FORECASTFOUNDRY_DATABASE_URL", database_url)
    cli = await execute_cli(argparse.Namespace(command="portfolio", json=True))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/portfolio")
    assert response.status_code == 200
    rest = response.json()
    for payload in (mcp, cli, rest):
        assert payload["paper_orders"] == direct["paper_orders"] == 1
        assert payload["paper_fills"] == direct["paper_fills"] == 1
        assert payload["open_positions"] == direct["open_positions"] == 1
        assert payload["paper_balance"] == direct["paper_balance"]
    await engine.dispose()
