import argparse
import asyncio
import sqlite3
from dataclasses import replace
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
    ApplicationSetting,
    Base,
    CalibrationMetric,
    DomainContract,
    EvidenceSnapshot,
    ExecutionControlState,
    ExecutionFill,
    ExecutionOrder,
    PaperPosition,
    PaperSettlement,
    PredictionRun,
    Signal,
)
from app.services.application import ApplicationServices
from app.services.crypto_data import (
    _endpoint,
    canonical_payload_hash,
    normalize_crypto_settlement_payload,
)
from app.services.paper import (
    PaperLifecycle,
    SettlementEvidence,
    SettlementWorker,
    get_paper_balance,
)

ENTRY_NOW = datetime(2026, 8, 3, 23, tzinfo=UTC)


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
            contract_data={
                "asset": "BTC",
                "quote": "USD",
                "source": "coinbase",
                "comparison": "above",
                "comparison_inclusive": False,
                "threshold": "100",
                "price_definition": "closing price",
                "rounding_increment": "1",
                "rounding_mode": "half_up",
            },
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
            normalized_values={"asset": "BTC", "quote": "USD", "price": "100"},
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
            signal_data={
                "candidate": {"required_size": "5"},
                "market": {"active": True, "closed": False},
            },
        )
        session.add(signal)
        session.add(
            ExecutionControlState(
                id=1,
                paused=False,
                revision=0,
                request_id="test-bootstrap",
                actor="test",
                reason="test entries allowed",
                updated_at=datetime(2026, 8, 3, tzinfo=UTC),
            )
        )
        await session.commit()
    return engine, sessions, signal.id, contract.id, expiry


def _crypto_evidence(
    contract_id: int,
    expiry: datetime,
    price: str,
    *,
    claimed: str | None = None,
    source: str = "coinbase",
    retrieved_at: datetime | None = None,
) -> SettlementEvidence:
    url, query = _endpoint("coinbase", "BTC", "USD", "1h", 200)
    payload = {
        "request": {"url": url, "query": query},
        "response": [
            [
                int((expiry - timedelta(hours=1)).timestamp()),
                price,
                price,
                price,
                price,
                "1",
            ]
        ],
    }
    normalized = normalize_crypto_settlement_payload(
        "coinbase", payload, asset="BTC", quote="USD", expiry=expiry
    )
    return SettlementEvidence(
        contract_id=contract_id,
        source=source,
        observed_at=expiry,
        retrieved_at=retrieved_at or expiry,
        outcome_label=claimed,
        raw_response_hash=canonical_payload_hash(payload),
        raw_payload=payload,
        normalized_values=normalized,
        provider_version="fixture-v1",
    )


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

    first = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    retry = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    assert first == retry
    assert first["status"] == "filled"

    evidence = _crypto_evidence(
        contract_id,
        expiry,
        "99.49",
        claimed="NO",
        retrieved_at=expiry + timedelta(minutes=1),
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
    assert settled["won"] is False

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
        assert position is not None and position.realized_pnl < 0
    await restarted_engine.dispose()


@pytest.mark.asyncio
async def test_risk_denial_is_persisted_without_financial_mutation(tmp_path: Path) -> None:
    engine, sessions, signal_id, _, _ = await _seed(tmp_path, balance=Decimal("5"))
    settings = Settings(
        app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("5")
    )
    result = await PaperLifecycle(sessions, settings).execute_signal(signal_id, now=ENTRY_NOW)
    retry = await PaperLifecycle(sessions, settings).execute_signal(signal_id, now=ENTRY_NOW)
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
    execution = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    evidence = _crypto_evidence(contract_id, expiry, "101", claimed="YES", source="binance")
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
    execution = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    fetched: list[int] = []

    async def fetch(position_id: int) -> SettlementEvidence:
        fetched.append(position_id)
        return _crypto_evidence(contract_id, expiry, "99", claimed="NO")

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
    execution = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    await lifecycle.settle_position(
        execution["position_id"],
        _crypto_evidence(contract_id, expiry, "99", claimed="NO"),
    )
    async with sessions() as session:
        metric = await session.scalar(select(CalibrationMetric))
        assert metric is not None
        assert metric.brier_score == Decimal("0.04")
    await engine.dispose()


async def _add_second_signal(sessions, *, fingerprint: str = "signal-fp-2") -> int:
    async with sessions() as session:
        first = await session.scalar(select(Signal).where(Signal.fingerprint == "signal-fp"))
        assert first is not None
        second = Signal(
            contract_id=first.contract_id,
            prediction_run_id=first.prediction_run_id,
            evidence_snapshot_id=first.evidence_snapshot_id,
            side="buy",
            outcome_label=first.outcome_label,
            token_id="yes-token-2",
            generated_at=first.generated_at,
            model_probability=first.model_probability,
            executable_ask=first.executable_ask,
            raw_edge=first.raw_edge,
            usable_edge=first.usable_edge,
            buffers=first.buffers,
            fingerprint=fingerprint,
            freshness_seconds=first.freshness_seconds,
            signal_data=first.signal_data,
        )
        session.add(second)
        await session.commit()
        return second.id


@pytest.mark.asyncio
async def test_concurrent_distinct_entries_reserve_exact_balance(tmp_path: Path) -> None:
    engine, sessions, first_id, _, _ = await _seed(tmp_path)
    second_id = await _add_second_signal(sessions)
    settings = Settings(
        app_env="test",
        scheduler_enabled=False,
        paper_starting_balance=Decimal("100"),
        estimated_fee=Decimal("0.01"),
        slippage_buffer=Decimal("0.01"),
    )
    lifecycle = PaperLifecycle(sessions, settings)
    results = await asyncio.gather(
        lifecycle.execute_signal(first_id, now=ENTRY_NOW),
        lifecycle.execute_signal(second_id, now=ENTRY_NOW),
    )
    assert {result["status"] for result in results} == {"filled"}
    async with sessions() as session:
        assert await get_paper_balance(session, Decimal("100")) == Decimal("95.859")
        assert await session.scalar(
            select(func.sum(PaperPosition.amount)).where(PaperPosition.status == "open")
        ) == Decimal("4.141")
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_shared_evidence_settles_two_positions_once(tmp_path: Path) -> None:
    engine, sessions, first_id, contract_id, expiry = await _seed(tmp_path)
    second_id = await _add_second_signal(sessions)
    settings = Settings(
        app_env="test",
        scheduler_enabled=False,
        paper_starting_balance=Decimal("100"),
        estimated_fee=Decimal("0.01"),
        slippage_buffer=Decimal("0.01"),
    )
    lifecycle = PaperLifecycle(sessions, settings)
    entries = await asyncio.gather(
        lifecycle.execute_signal(first_id, now=ENTRY_NOW),
        lifecycle.execute_signal(second_id, now=ENTRY_NOW),
    )
    evidence = _crypto_evidence(contract_id, expiry, "100.50", claimed="YES")
    settled = await asyncio.gather(
        *(lifecycle.settle_position(int(entry["position_id"]), evidence) for entry in entries)
    )
    assert all(result["won"] for result in settled)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperSettlement)) == 2
        assert await session.scalar(select(func.count()).select_from(EvidenceSnapshot)) == 2
        assert await get_paper_balance(session, Decimal("100")) == Decimal("105.859")
    await engine.dispose()


@pytest.mark.asyncio
async def test_paused_and_stale_signals_are_typed_denials_without_orders(tmp_path: Path) -> None:
    engine, sessions, signal_id, _, _ = await _seed(tmp_path)
    async with sessions() as session:
        control = await session.get(ExecutionControlState, 1)
        assert control is not None
        control.paused = True
        await session.commit()
    lifecycle = PaperLifecycle(
        sessions,
        Settings(app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")),
    )
    paused = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    assert paused["reasons"] == ["execution_paused"]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionOrder)) == 0
        assert await session.get(ApplicationSetting, "paper_balance") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_changed_execution_arguments_conflict(tmp_path: Path) -> None:
    engine, sessions, signal_id, _, _ = await _seed(tmp_path)
    lifecycle = PaperLifecycle(
        sessions,
        Settings(app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")),
    )
    await lifecycle.execute_signal(signal_id, requested_shares=Decimal("5"), now=ENTRY_NOW)
    with pytest.raises(RuntimeError, match="idempotency"):
        await lifecycle.execute_signal(signal_id, requested_shares=Decimal("6"), now=ENTRY_NOW)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("contract_missing", "contract_missing"),
        ("contract_rejected", "contract_not_accepted"),
        ("expired", "contract_expired"),
        ("closed", "market_closed"),
        ("inactive", "market_inactive"),
        ("stale", "signal_stale"),
        ("signal_future", "signal_from_future"),
        ("prediction_future", "prediction_from_future"),
        ("evidence_retrieval_future", "evidence_retrieved_from_future"),
        ("evidence_source_future", "evidence_source_from_future"),
    ),
)
async def test_entry_eligibility_fails_closed(tmp_path: Path, case: str, reason: str) -> None:
    engine, sessions, signal_id, _, expiry = await _seed(tmp_path)
    now = ENTRY_NOW
    async with sessions() as session:
        signal = await session.get(Signal, signal_id)
        assert signal is not None
        contract = await session.get(DomainContract, signal.contract_id)
        assert contract is not None
        prediction = await session.get(PredictionRun, signal.prediction_run_id)
        assert prediction is not None
        evidence = await session.get(EvidenceSnapshot, signal.evidence_snapshot_id)
        assert evidence is not None
        if case == "contract_missing":
            signal.contract_id = None
        elif case == "contract_rejected":
            contract.accepted = False
        elif case == "expired":
            now = expiry
        elif case == "closed":
            signal.signal_data = {
                **signal.signal_data,
                "market": {"active": True, "closed": True},
            }
        elif case == "inactive":
            signal.signal_data = {
                **signal.signal_data,
                "market": {"active": False, "closed": False},
            }
        elif case == "stale":
            now = expiry - timedelta(minutes=10)
        elif case == "signal_future":
            signal.generated_at = now + timedelta(seconds=1)
        elif case == "prediction_future":
            prediction.generated_at = now + timedelta(seconds=1)
        elif case == "evidence_retrieval_future":
            evidence.retrieved_at = now + timedelta(seconds=1)
        elif case == "evidence_source_future":
            evidence.source_timestamp = now + timedelta(seconds=1)
        await session.commit()
    result = await PaperLifecycle(
        sessions,
        Settings(app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")),
    ).execute_signal(signal_id, now=now)
    assert reason in result["reasons"]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionOrder)) == 0
        assert await session.get(ApplicationSetting, "paper_balance") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_entry_rejects_naive_now_without_mutation(tmp_path: Path) -> None:
    engine, sessions, signal_id, _, _ = await _seed(tmp_path)
    lifecycle = PaperLifecycle(
        sessions,
        Settings(app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")),
    )
    with pytest.raises(ValueError, match="timezone"):
        await lifecycle.execute_signal(signal_id, now=datetime(2026, 8, 3, 12))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionOrder)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_crypto_settlement_derives_equality_and_rejects_changed_replay(
    tmp_path: Path,
) -> None:
    engine, sessions, signal_id, contract_id, expiry = await _seed(tmp_path)
    lifecycle = PaperLifecycle(
        sessions,
        Settings(app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")),
    )
    entry = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    evidence = _crypto_evidence(contract_id, expiry, "100")
    result = await lifecycle.settle_position(int(entry["position_id"]), evidence)
    assert result["outcome"] == "NO"
    changed = _crypto_evidence(contract_id, expiry, "101", claimed="YES")
    with pytest.raises(RuntimeError, match="idempotency"):
        await lifecycle.settle_position(int(entry["position_id"]), changed)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"source": "binance"}, "source"),
        ({"asset": "ETH"}, "asset"),
        ({"quote": "EUR"}, "quote"),
        ({"observed_delta": 1}, "timestamp"),
        ({"claimed": "YES", "price": "99"}, "claimed outcome"),
        ({"raw_payload": None}, "raw payload is required"),
        ({"raw_hash": "not-a-sha256"}, "raw payload hash is invalid"),
        ({"raw_payload": {"price": "99"}}, "raw payload hash"),
    ),
)
async def test_crypto_settlement_rejects_forged_or_mismatched_evidence(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    engine, sessions, signal_id, contract_id, expiry = await _seed(tmp_path)
    lifecycle = PaperLifecycle(
        sessions,
        Settings(app_env="test", scheduler_enabled=False, paper_starting_balance=Decimal("100")),
    )
    entry = await lifecycle.execute_signal(signal_id, now=ENTRY_NOW)
    evidence = _crypto_evidence(contract_id, expiry, str(change.get("price", "101")))
    if "source" in change:
        evidence = replace(evidence, source=str(change["source"]))
    if "asset" in change or "quote" in change:
        evidence = replace(
            evidence,
            normalized_values={
                **evidence.normalized_values,
                **{key: str(change[key]) for key in ("asset", "quote") if key in change},
            },
        )
    if "observed_delta" in change:
        changed_time = expiry + timedelta(seconds=int(change["observed_delta"]))
        evidence = replace(evidence, observed_at=changed_time, retrieved_at=changed_time)
    if "claimed" in change:
        evidence = replace(evidence, outcome_label=str(change["claimed"]))
    if "raw_payload" in change:
        evidence = replace(evidence, raw_payload=change["raw_payload"])
    if "raw_hash" in change:
        evidence = replace(evidence, raw_response_hash=str(change["raw_hash"]))
    with pytest.raises(ValueError, match=message):
        await lifecycle.settle_position(int(entry["position_id"]), evidence)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaperSettlement)) == 0
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
    asyncio.run(_populate_migrated_crypto_lifecycle(database_path))
    with pytest.raises(RuntimeError, match="0008 downgrade refused before schema changes"):
        command.downgrade(config, "0007")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0008",
        )
        assert (
            connection.execute(
                "SELECT value FROM application_settings WHERE key = 'paper_balance'"
            ).fetchone()
            is not None
        )
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone() == (1,)
        for table in (
            "execution_orders",
            "execution_fills",
            "paper_positions",
            "paper_settlements",
            "calibration_metrics",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
        position_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(paper_positions)")
        }
        assert "execution_order_id" in position_columns


def test_paper_lifecycle_migration_empty_downgrade_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "paper-empty-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
    command.downgrade(config, "0007")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0007",
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_positions)")}
        assert "execution_order_id" not in columns
    command.upgrade(config, "head")


async def _populate_migrated_crypto_lifecycle(database_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    sessions = make_session_factory(engine)
    expiry = datetime(2026, 8, 4, tzinfo=UTC)
    async with sessions() as session:
        contract = DomainContract(
            market_external_id="migration-crypto",
            domain="crypto",
            accepted=True,
            resolution_source="coinbase",
            expiry=expiry,
            contract_data={
                "asset": "BTC",
                "quote": "USD",
                "source": "coinbase",
                "comparison": "above",
                "comparison_inclusive": False,
                "threshold": "100",
                "price_definition": "closing price",
                "rounding_increment": "1",
                "rounding_mode": "half_up",
            },
            rejection_reasons=[],
            provenance={},
            fingerprint="migration-contract",
        )
        session.add(contract)
        await session.flush()
        evidence = EvidenceSnapshot(
            contract_id=contract.id,
            fingerprint="migration-evidence",
            provider="coinbase",
            source_timestamp=expiry - timedelta(hours=1),
            retrieved_at=expiry - timedelta(hours=1),
            raw_response_hash="migration-raw",
            normalized_values={
                "asset": "BTC",
                "quote": "USD",
                "freshness_limit_seconds": 7200,
            },
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
            model_name="migration",
            model_version="v1",
            input_hash="migration-input",
            parameters={},
            probabilities={"YES": "0.8"},
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
            token_id="migration-token",
            generated_at=expiry - timedelta(hours=1),
            model_probability=Decimal("0.8"),
            executable_ask=Decimal("0.1"),
            raw_edge=Decimal("0.7"),
            usable_edge=Decimal("0.6"),
            buffers={},
            fingerprint="migration-signal",
            freshness_seconds=0,
            signal_data={
                "candidate": {"required_size": "5"},
                "market": {"active": True, "closed": False},
            },
        )
        session.add(signal)
        control = await session.get(ExecutionControlState, 1)
        assert control is not None
        control.paused = False
        control.request_id = "migration-control"
        control.actor = "test"
        control.reason = "migration"
        control.updated_at = expiry - timedelta(hours=1)
        await session.commit()
    lifecycle = PaperLifecycle(
        sessions,
        Settings(
            app_env="test",
            scheduler_enabled=False,
            paper_starting_balance=Decimal("17.5"),
        ),
    )
    entry = await lifecycle.execute_signal(signal.id, now=expiry - timedelta(hours=1))
    await lifecycle.settle_position(
        int(entry["position_id"]),
        _crypto_evidence(contract.id, expiry, "101"),
    )
    await engine.dispose()


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
    await PaperLifecycle(sessions, settings).execute_signal(signal_id, now=ENTRY_NOW)
    async with sessions() as session:
        live_order = ExecutionOrder(
            mode="live",
            provider="fixture",
            client_order_id="live-order",
            provider_order_id="live-provider-order",
            side="buy",
            price=Decimal("0.5"),
            size=Decimal("1"),
            status="filled",
            live_authorized=True,
            metadata_json={},
        )
        session.add(live_order)
        await session.flush()
        session.add(
            ExecutionFill(
                execution_order_id=live_order.id,
                provider_fill_id="live-fill",
                filled_at=datetime.now(UTC),
                price=Decimal("0.5"),
                size=Decimal("1"),
                fee=Decimal("0"),
                raw_data={},
            )
        )
        await session.commit()
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
