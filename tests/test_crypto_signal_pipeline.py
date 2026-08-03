import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select

from alembic import command
from app.config import Settings
from app.database import make_engine, make_session_factory
from app.domains.base import MarketInput
from app.models import (
    Base,
    CalibrationMetric,
    DomainContract,
    EvidenceSnapshot,
    ExecutionControlState,
    ExecutionFill,
    ExecutionOrder,
    PaperExecutionDecision,
    PaperPosition,
    PaperSettlement,
    PredictionRun,
    RejectedSignal,
    Signal,
)
from app.schemas import GammaMarket, OrderBook, OrderLevel
from app.services.crypto_data import (
    CryptoCandle,
    CryptoDataQualityError,
    CryptoSeries,
    _endpoint,
    _rows,
    canonical_payload_hash,
    normalize_candles,
    normalize_crypto_settlement_payload,
)
from app.services.crypto_pipeline import CryptoPaperPipeline
from app.services.paper import PaperLifecycle, SettlementEvidence
from app.services.polymarket import parse_order_book

NOW = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "crypto" / "signal_case.json").read_text(encoding="utf-8")
)


def _gamma_market(expiry: datetime) -> GammaMarket:
    return GammaMarket.model_validate(FIXTURE["market"]).model_copy(
        update={
            "question": f"Will BTC be above $90 at {expiry:%Y-%m-%d %H:%M} UTC?",
            "end_date": expiry,
        }
    )


def _market(
    *,
    expiry: datetime,
    threshold: Decimal = Decimal("90"),
    source: str = "coinbase",
) -> MarketInput:
    gamma = _gamma_market(expiry).model_copy(
        update={
            "question": f"Will BTC be above ${threshold} at {expiry:%Y-%m-%d %H:%M} UTC?",
            "description": (f"{source.title()} BTC-USD closing price, rounded to nearest dollar."),
            "resolution_source": source,
        }
    )
    return MarketInput(
        market_id=gamma.id,
        title=gamma.question,
        description=gamma.description,
        raw_data={"market": gamma.model_dump(mode="json")},
    )


def _book(
    token: str,
    *,
    ask: str,
    bid: str,
    timestamp: datetime = NOW,
) -> OrderBook:
    best_ask = Decimal(ask)
    best_bid = Decimal(bid)
    return OrderBook(
        condition_id="crypto-condition",
        asset_id=token,
        timestamp=str(int(timestamp.timestamp() * 1000)),
        bids=(OrderLevel(price=best_bid, size=Decimal("100")),),
        asks=(OrderLevel(price=best_ask, size=Decimal("100")),),
        best_bid=best_bid,
        best_ask=best_ask,
        spread=best_ask - best_bid,
        midpoint=(best_ask + best_bid) / 2,
        available_depth=Decimal("100"),
        minimum_order_size=Decimal("5"),
        tick_size=Decimal("0.01"),
        raw_data={"fixture": token},
    )


def _depth_book(
    token: str,
    asks: tuple[tuple[str, str], ...],
    *,
    minimum_order_size: str = "5",
    condition_id: str = "crypto-condition",
) -> OrderBook:
    ask_levels = tuple(OrderLevel(price=Decimal(price), size=Decimal(size)) for price, size in asks)
    best_ask = ask_levels[0].price
    best_bid = best_ask - Decimal("0.02")
    return OrderBook(
        condition_id=condition_id,
        asset_id=token,
        timestamp=str(int(NOW.timestamp() * 1000)),
        bids=(OrderLevel(price=best_bid, size=Decimal("100")),),
        asks=ask_levels,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=Decimal("0.02"),
        midpoint=(best_bid + best_ask) / 2,
        available_depth=sum((level.size for level in ask_levels), Decimal("0")),
        minimum_order_size=Decimal(minimum_order_size),
        tick_size=Decimal("0.01"),
        raw_data={"fixture": token, "asks": asks},
    )


class FixtureCryptoData:
    def __init__(
        self,
        *,
        rows: Sequence[CryptoCandle] | None = None,
        freshness: timedelta = timedelta(hours=2),
        min_history: int = 5,
    ) -> None:
        self.rows = rows or tuple(
            CryptoCandle(
                datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                Decimal(row["close"]),
            )
            for row in FIXTURE["candles"]
        )
        self.freshness = freshness
        self.min_history = min_history

    async def fetch_series(
        self,
        source: str,
        *,
        asset: str,
        quote: str,
        now: datetime,
        granularity: str = "1h",
        limit: int = 200,
    ) -> CryptoSeries:
        assert (asset, quote, granularity, limit) == ("BTC", "USD", "1h", 200)
        return normalize_candles(
            source,
            self.rows,
            now=now,
            interval=timedelta(hours=1),
            freshness=self.freshness,
            min_history=self.min_history,
            provider_version=f"{source}-fixture-v1",
            raw_response={"source": source, "rows": [str(row) for row in self.rows]},
            request_url=f"https://fixture.test/{source}/candles",
            request_params={"pair": f"{asset}{quote}", "interval": granularity},
        )


class FixtureBooks:
    def __init__(self, books: Sequence[OrderBook]) -> None:
        self.books = tuple(books)
        self.requested: tuple[str, ...] = ()

    async def get_order_books(self, token_ids: Sequence[str]) -> tuple[OrderBook, ...]:
        self.requested = tuple(token_ids)
        return self.books


def _settings(database_url: str) -> Settings:
    return Settings(
        app_env="test",
        database_url=database_url,
        min_usable_edge=Decimal("0.05"),
        max_spread=Decimal("0.10"),
        min_liquidity_usd=Decimal("100"),
        estimated_fee=Decimal("0.01"),
        slippage_buffer=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.01"),
        rule_risk_buffer=Decimal("0.01"),
        paper_starting_balance=Decimal("100"),
    )


async def _pipeline(
    tmp_path: Path,
    *,
    data: FixtureCryptoData | None = None,
    books: Sequence[OrderBook] | None = None,
) -> tuple[CryptoPaperPipeline, object, FixtureBooks]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'crypto-signals.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        session.add(
            ExecutionControlState(
                id=1,
                paused=False,
                revision=0,
                request_id="crypto-test",
                actor="test",
                reason="test entries allowed",
                updated_at=NOW,
            )
        )
        await session.commit()
    pricing = FixtureBooks(
        books or tuple(OrderBook.model_validate(book) for book in FIXTURE["books"])
    )
    pipeline = CryptoPaperPipeline(
        sessions,
        data or FixtureCryptoData(),
        pricing=pricing,
        settings=_settings(database_url),
    )
    return pipeline, engine, pricing


@pytest.mark.asyncio
async def test_accepts_yes_with_complete_reproducible_evidence_and_horizon(tmp_path: Path) -> None:
    pipeline, engine, pricing = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))

    result = await pipeline.run(market, now=NOW, seed=7, samples=500)

    assert result["status"] == "accepted"
    assert result["outcome"] == "YES"
    assert result["paper"]["status"] == "filled"
    assert pricing.requested == ("yes-token", "no-token")
    async with pipeline.sessions() as session:
        contract = await session.scalar(select(DomainContract))
        evidence = await session.scalar(select(EvidenceSnapshot))
        prediction = await session.scalar(select(PredictionRun))
        signal = await session.scalar(select(Signal))
    assert contract is not None and evidence is not None and prediction is not None
    assert signal is not None
    assert evidence.contract_id == contract.id
    assert evidence.normalized_values["candles"][-1]["close"] == "101"
    assert evidence.normalized_values["interval_seconds"] == 3600
    assert evidence.normalized_values["request"] == {
        "url": "https://fixture.test/coinbase/candles",
        "query": {"interval": "1h", "pair": "BTCUSD"},
    }
    assert len(evidence.normalized_values["market_acquisition"]["gamma_market_hash"]) == 64
    assert evidence.license_metadata["evidence_role"] == "authoritative_resolution"
    assert evidence.license_metadata["named_resolution_source"] == "coinbase"
    assert prediction.parameters["horizon"] == 4
    assert prediction.parameters["seed"] == 7
    assert prediction.parameters["samples"] == 500
    assert prediction.parameters["interval_seconds"] == 3600
    assert prediction.probabilities.keys() >= {
        "event",
        "bootstrap",
        "monte_carlo",
    }
    assert signal.contract_id == contract.id
    assert signal.prediction_run_id == prediction.id
    assert signal.evidence_snapshot_id == evidence.id
    assert signal.side == "buy"
    assert signal.outcome_label == "YES"
    assert signal.token_id == "yes-token"
    assert signal.buffers.keys() >= {
        "estimated_fee",
        "slippage",
        "uncertainty",
        "rule_risk",
        "liquidity",
        "spread",
    }
    assert signal.freshness_seconds >= 0
    assert signal.signal_data["policy"]["version"] == "crypto-signal-v2"
    await engine.dispose()


@pytest.mark.asyncio
async def test_btc_fixture_runs_through_settlement_and_calibration(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    expiry = datetime(2026, 8, 3, 16, tzinfo=UTC)

    result = await pipeline.run(_market(expiry=expiry), now=NOW, seed=7, samples=500)
    assert result["paper"]["status"] == "filled"
    async with pipeline.sessions() as session:
        contract = await session.scalar(select(DomainContract))
    assert contract is not None
    url, query = _endpoint("coinbase", "BTC", "USD", "1h", 200)
    payload = {
        "request": {"url": url, "query": query},
        "response": [
            [
                int((expiry - timedelta(hours=1)).timestamp()),
                "101",
                "101",
                "101",
                "101",
                "1",
            ]
        ],
    }
    settlement = await PaperLifecycle(pipeline.sessions, pipeline.settings).settle_position(
        int(result["paper"]["position_id"]),
        SettlementEvidence(
            contract_id=contract.id,
            source="coinbase",
            observed_at=expiry,
            retrieved_at=expiry + timedelta(minutes=1),
            outcome_label="YES",
            raw_response_hash=canonical_payload_hash(payload),
            raw_payload=payload,
            normalized_values=normalize_crypto_settlement_payload(
                "coinbase", payload, asset="BTC", quote="USD", expiry=expiry
            ),
            provider_version="coinbase-fixture-v1",
        ),
    )
    assert settlement["won"] is True
    async with pipeline.sessions() as session:
        for model in (
            ExecutionOrder,
            ExecutionFill,
            PaperPosition,
            PaperSettlement,
            CalibrationMetric,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_accepts_no_using_no_ask_and_one_minus_yes_probability(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(
        tmp_path,
        books=(
            _book("yes-token", ask="0.95", bid="0.93"),
            _book("no-token", ask="0.35", bid="0.33"),
        ),
    )

    result = await pipeline.run(
        _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC), threshold=Decimal("200")),
        now=NOW,
        seed=7,
        samples=500,
    )

    assert result["status"] == "accepted"
    assert result["outcome"] == "NO"
    async with pipeline.sessions() as session:
        signal = await session.scalar(select(Signal))
    assert signal is not None
    assert signal.outcome_label == "NO"
    assert signal.token_id == "no-token"
    assert signal.executable_ask == Decimal("0.35")
    assert signal.model_probability > Decimal("0.90")
    await engine.dispose()


@pytest.mark.asyncio
async def test_negative_edges_persist_one_rejection_and_never_become_yes_trade(
    tmp_path: Path,
) -> None:
    pipeline, engine, _ = await _pipeline(
        tmp_path,
        books=(
            _book("yes-token", ask="0.99", bid="0.97"),
            _book("no-token", ask="0.99", bid="0.97"),
        ),
    )
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))

    first = await pipeline.run(market, now=NOW, seed=7, samples=500)
    second = await pipeline.run(market, now=NOW, seed=7, samples=500)

    assert first == second
    assert first["status"] == "rejected"
    assert "usable_edge_below_minimum" in first["reasons"]
    async with pipeline.sessions() as session:
        signals = await session.scalar(select(func.count()).select_from(Signal))
        rejected = (await session.scalars(select(RejectedSignal))).all()
        evidence = await session.scalar(select(func.count()).select_from(EvidenceSnapshot))
        predictions = await session.scalar(select(func.count()).select_from(PredictionRun))
    assert signals == 0
    assert len(rejected) == 1
    assert rejected[0].outcome_label == "YES"
    assert rejected[0].usable_edge is not None and rejected[0].usable_edge < 0
    assert evidence == 1
    assert predictions == 1
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expiry", "reason"),
    [
        (
            FixtureCryptoData(
                rows=(
                    CryptoCandle(NOW.replace(minute=0) - timedelta(hours=5), Decimal("100")),
                    CryptoCandle(NOW.replace(minute=0) - timedelta(hours=4), Decimal("101")),
                ),
            ),
            datetime(2026, 8, 3, 16, tzinfo=UTC),
            "insufficient_history",
        ),
        (
            FixtureCryptoData(
                rows=tuple(
                    CryptoCandle(
                        NOW.replace(minute=0) - timedelta(hours=20 - index),
                        Decimal(100 + index),
                    )
                    for index in range(6)
                ),
                freshness=timedelta(hours=1),
            ),
            datetime(2026, 8, 3, 16, tzinfo=UTC),
            "stale_data",
        ),
        (
            FixtureCryptoData(),
            datetime(2026, 8, 3, 12, tzinfo=UTC),
            "expired_contract",
        ),
        (
            FixtureCryptoData(),
            datetime(2026, 8, 3, 16, 30, tzinfo=UTC),
            "unsupported_horizon",
        ),
    ],
)
async def test_quality_expiry_and_horizon_fail_closed(
    tmp_path: Path,
    data: FixtureCryptoData,
    expiry: datetime,
    reason: str,
) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path, data=data)

    result = await pipeline.run(_market(expiry=expiry), now=NOW)

    assert result["status"] == "rejected"
    assert reason in result["reasons"]
    async with pipeline.sessions() as session:
        rejected = await session.scalar(select(RejectedSignal))
        signals = await session.scalar(select(func.count()).select_from(Signal))
    assert rejected is not None and reason in rejected.reasons
    assert rejected.contract_id is not None
    assert signals == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_named_kraken_source_is_authoritative_without_relabeling_registry(
    tmp_path: Path,
) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)

    result = await pipeline.run(
        _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC), source="kraken"),
        now=NOW,
        seed=7,
        samples=500,
    )

    assert result["status"] == "accepted"
    async with pipeline.sessions() as session:
        evidence = await session.scalar(select(EvidenceSnapshot))
    assert evidence is not None
    assert evidence.provider == "kraken"
    assert evidence.license_metadata == {
        "attribution": "Kraken",
        "provider_classification": "corroborating",
        "evidence_role": "authoritative_resolution",
        "named_resolution_source": "kraken",
    }
    await engine.dispose()


def test_provider_pair_mapping_is_exact() -> None:
    kraken_url, kraken_params = _endpoint("kraken", "BTC", "USD", "1h", 200)
    assert kraken_url.endswith("/OHLC")
    assert kraken_params == {"pair": "XBTUSD", "interval": 60}

    with pytest.raises(ValueError, match="unsupported_pair"):
        _endpoint("binance", "BTC", "USD", "1h", 200)


def test_candle_order_and_incomplete_interval_are_validated() -> None:
    rows = tuple(
        CryptoCandle(NOW.replace(minute=0) - timedelta(hours=hours), Decimal(price))
        for hours, price in ((3, "100"), (2, "101"), (1, "102"), (0, "999"))
    )
    series = normalize_candles(
        "coinbase",
        tuple(reversed(rows)),
        now=NOW,
        interval=timedelta(hours=1),
        min_history=3,
    )
    assert [candle.close for candle in series.candles] == [
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
    ]
    assert series.quality_flags == (
        "source_descending_reversed",
        "incomplete_current_candle_removed",
    )

    with pytest.raises(CryptoDataQualityError, match="out_of_order_candles"):
        normalize_candles(
            "binance",
            (rows[1], rows[0], rows[2], rows[3]),
            now=NOW,
            interval=timedelta(hours=1),
            min_history=3,
        )


def test_crypto_signal_migration_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "crypto-signal-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        evidence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(evidence_snapshots)")
        }
        signal_columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
        rejected_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(rejected_signals)")
        }
    assert {"contract_id", "fingerprint"} <= evidence_columns
    assert {
        "contract_id",
        "prediction_run_id",
        "evidence_snapshot_id",
        "side",
        "outcome_label",
        "token_id",
        "freshness_seconds",
    } <= signal_columns
    assert {
        "contract_id",
        "prediction_run_id",
        "evidence_snapshot_id",
        "fingerprint",
        "model_probability",
        "executable_ask",
        "raw_edge",
        "usable_edge",
        "buffers",
        "freshness_seconds",
    } <= rejected_columns

    command.downgrade(config, "0006")
    with sqlite3.connect(database_path) as connection:
        signal_columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert "contract_id" not in signal_columns


def test_crypto_signal_migration_consolidates_duplicate_predictions_with_audit_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "crypto-signal-duplicate-predictions.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "0006")
    created = NOW.isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO domain_contracts "
            "(market_external_id, domain, accepted, contract_data, rejection_reasons, "
            "provenance, fingerprint, created_at, updated_at) VALUES "
            "('duplicate-prediction', 'crypto', 1, '{}', '[]', '{}', 'contract-duplicate', ?, ?)",
            (created, created),
        )
        contract_id = connection.execute(
            "SELECT id FROM domain_contracts WHERE fingerprint = 'contract-duplicate'"
        ).fetchone()[0]
        for model_version in ("original", "retry"):
            connection.execute(
                "INSERT INTO prediction_runs "
                "(contract_id, generated_at, model_name, model_version, input_hash, parameters, "
                "probabilities, status) VALUES (?, ?, 'crypto', ?, 'same-input', '{}', '{}', "
                "'signal_candidate')",
                (contract_id, created, model_version),
            )
        prediction_ids = [
            row[0]
            for row in connection.execute("SELECT id FROM prediction_runs ORDER BY id").fetchall()
        ]
        connection.execute(
            "INSERT INTO prediction_features "
            "(prediction_run_id, name, value, source, live_eligible) VALUES "
            "(?, 'return_history', '[1]', 'coinbase', 1), "
            "(?, 'return_history', '[2]', 'coinbase', 1)",
            prediction_ids,
        )
        for prediction_id, score in zip(prediction_ids, ("0.10", "0.20"), strict=True):
            connection.execute(
                "INSERT INTO calibration_metrics "
                "(prediction_run_id, model_name, window_start, window_end, brier_score, "
                "reliability_buckets) VALUES (?, 'crypto', ?, ?, ?, '[]')",
                (prediction_id, created, created, score),
            )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        predictions = connection.execute(
            "SELECT id, model_version FROM prediction_runs WHERE contract_id = ? AND input_hash = "
            "'same-input'",
            (contract_id,),
        ).fetchall()
        features = connection.execute(
            "SELECT prediction_run_id, value FROM prediction_features ORDER BY id"
        ).fetchall()
        metrics = connection.execute(
            "SELECT prediction_run_id, brier_score FROM calibration_metrics ORDER BY id"
        ).fetchall()

    assert predictions == [(prediction_ids[0], "original")]
    assert features == [(prediction_ids[0], "[1]"), (prediction_ids[0], "[2]")]
    assert metrics == [(prediction_ids[0], 0.1), (prediction_ids[0], 0.2)]


@pytest.mark.asyncio
async def test_uses_minimum_size_vwap_across_multiple_ask_levels(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(
        tmp_path,
        books=(
            _depth_book("yes-token", (("0.40", "2"), ("0.50", "3"))),
            _depth_book("no-token", (("0.65", "100"),)),
        ),
    )

    result = await pipeline.run(
        _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC)),
        now=NOW,
        seed=7,
        samples=500,
    )

    assert result["status"] == "accepted"
    assert result["executable_price"] == "0.46"
    async with pipeline.sessions() as session:
        signal = await session.scalar(select(Signal))
    assert signal is not None
    assert signal.executable_ask == Decimal("0.46")
    assert signal.signal_data["candidate"]["required_size"] == "5"
    await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_real_book_levels_reproduce_minimum_size_vwap(tmp_path: Path) -> None:
    raw_books = json.loads(
        (Path(__file__).parent / "fixtures" / "crypto" / "multi_level_books.json").read_text(
            encoding="utf-8"
        )
    )
    books = tuple(parse_order_book(payload) for payload in raw_books)
    pipeline, engine, _ = await _pipeline(tmp_path, books=books)

    result = await pipeline.run(
        _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC)),
        now=NOW,
        seed=7,
        samples=500,
    )

    assert result["status"] == "accepted"
    async with pipeline.sessions() as session:
        signal = await session.scalar(select(Signal))
        evidence = await session.scalar(select(EvidenceSnapshot))
    assert signal is not None
    assert evidence is not None
    persisted = signal.signal_data
    assert persisted["pricing_acquisition"] == {
        "provider": "polymarket_clob",
        "method": "POST",
        "endpoint": "https://clob.polymarket.com/books",
        "request_payload": [{"token_id": "yes-token"}, {"token_id": "no-token"}],
    }
    selected = persisted["order_books"][persisted["candidate"]["outcome"]]
    remaining = Decimal(persisted["candidate"]["required_size"])
    cost = Decimal("0")
    for level in selected["asks"]:
        filled = min(remaining, Decimal(level["size"]))
        cost += filled * Decimal(level["price"])
        remaining -= filled
        if remaining == 0:
            break
    assert remaining == 0
    assert cost / Decimal(persisted["candidate"]["required_size"]) == signal.executable_ask
    assert selected["bids"] == [{"price": "0.38", "size": "100"}]
    assert selected["raw_response"] == raw_books[0]
    acquisition = evidence.normalized_values["market_acquisition"]
    assert acquisition["market_url"] == "https://gamma-api.polymarket.com/markets/crypto-market"
    assert acquisition["raw_response"]["id"] == "crypto-market"
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejects_when_asks_cannot_fill_minimum_size(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(
        tmp_path,
        books=(
            _depth_book("yes-token", (("0.40", "2"), ("0.50", "2"))),
            _depth_book("no-token", (("0.99", "100"),)),
        ),
    )

    result = await pipeline.run(_market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC)), now=NOW)

    assert result["status"] == "rejected"
    assert "insufficient_executable_depth" in result["reasons"]
    async with pipeline.sessions() as session:
        rejected = await session.scalar(select(RejectedSignal))
    assert rejected is not None
    assert rejected.candidate_data["pricing_acquisition"]["endpoint"].endswith("/books")
    assert rejected.candidate_data["order_books"]["YES"]["asks"] == [
        {"price": "0.40", "size": "2"},
        {"price": "0.50", "size": "2"},
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejects_duplicate_yes_no_token_ids(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))
    raw = dict(market.raw_data["market"])
    raw["token_ids"] = ["same-token", "same-token"]

    result = await pipeline.run(market.model_copy(update={"raw_data": {"market": raw}}), now=NOW)

    assert result["reasons"] == ["yes_no_outcome_mapping_invalid"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejects_empty_gamma_resolution_source(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))
    raw = dict(market.raw_data["market"])
    raw["resolution_source"] = ""

    result = await pipeline.run(market.model_copy(update={"raw_data": {"market": raw}}), now=NOW)

    assert result["reasons"] == ["resolution_source_missing"]
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution_source",
    (
        "Coinbase will not be used",
        "other than Coinbase",
        "instead of Coinbase",
        "Coinbase or Kraken",
    ),
)
async def test_rejects_noncanonical_gamma_resolution_source(
    tmp_path: Path, resolution_source: str
) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))
    raw = dict(market.raw_data["market"])
    raw["resolution_source"] = resolution_source

    result = await pipeline.run(market.model_copy(update={"raw_data": {"market": raw}}), now=NOW)

    assert result["status"] == "rejected"
    assert {
        "resolution_source_negated",
        "resolution_source_ambiguous",
        "resolution_source_missing",
    } & set(result["reasons"])
    await engine.dispose()


@pytest.mark.asyncio
async def test_zero_threshold_rejects_without_escaping_pipeline(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)

    result = await pipeline.run(
        _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC), threshold=Decimal("0")),
        now=NOW,
    )

    assert result["status"] == "rejected"
    assert result["reasons"] == ["threshold_non_positive"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_mapping_rejection_persists_returned_book_acquisition(tmp_path: Path) -> None:
    raw_books = json.loads(
        (Path(__file__).parent / "fixtures" / "crypto" / "multi_level_books.json").read_text(
            encoding="utf-8"
        )
    )
    extra = dict(raw_books[0])
    extra["asset_id"] = "unexpected-token"
    books = tuple(parse_order_book(payload) for payload in [*raw_books, extra])
    pipeline, engine, _ = await _pipeline(tmp_path, books=books)

    result = await pipeline.run(_market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC)), now=NOW)

    assert "order_book_token_set_mismatch" in result["reasons"]
    async with pipeline.sessions() as session:
        rejected = await session.scalar(select(RejectedSignal))
    assert rejected is not None
    acquisition = rejected.candidate_data["pricing_acquisition"]
    assert acquisition["request_payload"] == [
        {"token_id": "yes-token"},
        {"token_id": "no-token"},
    ]
    assert [book["token_id"] for book in rejected.candidate_data["returned_books"]] == [
        "yes-token",
        "no-token",
        "unexpected-token",
    ]
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("books", "reason"),
    [
        (
            (
                _book("yes-token", ask="0.40", bid="0.38"),
                _book("no-token", ask="0.65", bid="0.63"),
                _book("extra-token", ask="0.50", bid="0.48"),
            ),
            "order_book_token_set_mismatch",
        ),
        (
            (
                _depth_book("yes-token", (("0.40", "100"),), condition_id=""),
                _book("no-token", ask="0.65", bid="0.63"),
            ),
            "order_book_mismatch",
        ),
        (
            (
                _depth_book("yes-token", (("0.40", "100"),), minimum_order_size="4"),
                _book("no-token", ask="0.65", bid="0.63"),
            ),
            "minimum_order_size_mismatch",
        ),
    ],
)
async def test_rejects_inexact_book_mapping_and_minimums(
    tmp_path: Path, books: tuple[OrderBook, ...], reason: str
) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path, books=books)

    result = await pipeline.run(_market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC)), now=NOW)

    assert result["status"] == "rejected"
    assert reason in result["reasons"]
    await engine.dispose()


def test_candles_require_exact_interval_cadence_and_alignment() -> None:
    aligned = NOW.replace(minute=0)
    with pytest.raises(CryptoDataQualityError, match="candle_gap"):
        normalize_candles(
            "binance",
            (
                CryptoCandle(aligned - timedelta(hours=4), Decimal("100")),
                CryptoCandle(aligned - timedelta(hours=2), Decimal("101")),
                CryptoCandle(aligned - timedelta(hours=1), Decimal("102")),
            ),
            now=NOW,
            min_history=3,
        )
    with pytest.raises(CryptoDataQualityError, match="candle_misaligned"):
        normalize_candles(
            "binance",
            tuple(
                CryptoCandle(
                    aligned - timedelta(hours=hours) + timedelta(minutes=5),
                    Decimal(100 + hours),
                )
                for hours in (3, 2, 1)
            ),
            now=NOW,
            min_history=3,
        )


def test_kraken_decoder_requires_exact_requested_pair() -> None:
    row = [1785754800, "100", "101", "99", "100", "100", "1", 1]
    with pytest.raises(CryptoDataQualityError, match="provider_pair_mismatch"):
        _rows(
            "kraken",
            {"error": [], "result": {"ETHUSD": [row], "last": 1}},
            expected_pair="XXBTZUSD",
        )
    with pytest.raises(CryptoDataQualityError, match="provider_pair_ambiguous"):
        _rows(
            "kraken",
            {
                "error": [],
                "result": {"XBTUSD": [row], "XXBTZUSD": [row], "last": 1},
            },
            expected_pair="XXBTZUSD",
        )


def test_real_provider_candle_shapes_decode_to_exact_close_and_utc() -> None:
    coinbase = _rows("coinbase", [[1785754800, 90, 110, 95, "101", 5]])
    binance = _rows(
        "binance",
        [[1785754800000, "95", "110", "90", "102", "5", 1785758399999]],
    )
    kraken = _rows(
        "kraken",
        {
            "error": [],
            "result": {
                "XXBTZUSD": [[1785754800, "95", "110", "90", "103", "100", "5", 1]],
                "last": 1785754800,
            },
        },
        expected_pair="XXBTZUSD",
    )

    assert [rows[0]["close"] for rows in (coinbase, binance, kraken)] == [
        "101",
        "102",
        "103",
    ]
    assert all(rows[0]["timestamp"].tzinfo == UTC for rows in (coinbase, binance, kraken))


@pytest.mark.asyncio
async def test_rejects_naive_pipeline_time(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    with pytest.raises(ValueError, match="now_must_be_timezone_aware"):
        await pipeline.run(
            _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC)),
            now=datetime(2026, 8, 3, 12, 30),
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_pricing_policy_changes_create_distinct_immutable_decisions(
    tmp_path: Path,
) -> None:
    pipeline, engine, pricing = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))
    first = await pipeline.run(market, now=NOW, seed=7, samples=500)
    changed = CryptoPaperPipeline(
        pipeline.sessions,
        pipeline.data,
        pricing=pricing,
        settings=pipeline.settings.model_copy(update={"slippage_buffer": Decimal("0.02")}),
    )

    second = await changed.run(market, now=NOW, seed=7, samples=500)

    assert first["fingerprint"] != second["fingerprint"]
    async with pipeline.sessions() as session:
        signals = (await session.scalars(select(Signal).order_by(Signal.id))).all()
    assert len(signals) == 2
    assert signals[0].signal_data["policy"]["slippage"] == "0.01"
    assert signals[1].signal_data["policy"]["slippage"] == "0.02"
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_accepted_runs_are_idempotent(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))

    results = await asyncio.gather(
        pipeline.run(market, now=NOW, seed=7, samples=500),
        pipeline.run(market, now=NOW, seed=7, samples=500),
    )

    assert results[0] == results[1]
    async with pipeline.sessions() as session:
        counts = []
        for model in (
            EvidenceSnapshot,
            PredictionRun,
            Signal,
            PaperExecutionDecision,
            ExecutionOrder,
            ExecutionFill,
            PaperPosition,
        ):
            counts.append(await session.scalar(select(func.count()).select_from(model)))
    assert counts == [1, 1, 1, 1, 1, 1, 1]
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_rejected_runs_are_idempotent(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(
        tmp_path,
        books=(
            _book("yes-token", ask="0.99", bid="0.97"),
            _book("no-token", ask="0.99", bid="0.97"),
        ),
    )
    market = _market(expiry=datetime(2026, 8, 3, 16, tzinfo=UTC))

    results = await asyncio.gather(
        pipeline.run(market, now=NOW, seed=7, samples=500),
        pipeline.run(market, now=NOW, seed=7, samples=500),
    )

    assert results[0] == results[1]
    async with pipeline.sessions() as session:
        counts = []
        for model in (EvidenceSnapshot, PredictionRun, RejectedSignal):
            counts.append(await session.scalar(select(func.count()).select_from(model)))
    assert counts == [1, 1, 1]
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_contract_rejections_are_idempotent(tmp_path: Path) -> None:
    pipeline, engine, _ = await _pipeline(tmp_path)
    market = _market(expiry=datetime(2026, 8, 3, 12, tzinfo=UTC))

    results = await asyncio.gather(
        pipeline.run(market, now=NOW),
        pipeline.run(market, now=NOW),
    )

    assert results[0] == results[1]
    async with pipeline.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(RejectedSignal))
    assert count == 1
    await engine.dispose()


def test_crypto_signal_downgrade_discards_only_nonlegacy_signal_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "crypto-signal-downgrade.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
    created = NOW.isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO domain_contracts "
            "(market_external_id, domain, accepted, contract_data, rejection_reasons, "
            "provenance, fingerprint, created_at, updated_at) VALUES "
            "('crypto-downgrade', 'crypto', 1, '{}', '[]', '{}', 'contract-fp', ?, ?)",
            (created, created),
        )
        contract_id = connection.execute(
            "SELECT id FROM domain_contracts WHERE fingerprint = 'contract-fp'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO evidence_snapshots "
            "(contract_id, fingerprint, provider, retrieved_at, raw_response_hash, "
            "normalized_values, quality_flags, license_metadata) "
            "VALUES (?, 'evidence-fp', 'coinbase', ?, 'raw', '{}', '[]', '{}')",
            (contract_id, created),
        )
        evidence_id = connection.execute(
            "SELECT id FROM evidence_snapshots WHERE fingerprint = 'evidence-fp'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO prediction_runs "
            "(contract_id, evidence_snapshot_id, generated_at, model_name, model_version, "
            "input_hash, parameters, probabilities, status) "
            "VALUES (?, ?, ?, 'crypto', 'v1', 'input', '{}', '{}', 'signal_candidate')",
            (contract_id, evidence_id, created),
        )
        prediction_id = connection.execute(
            "SELECT id FROM prediction_runs WHERE input_hash = 'input'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO prediction_runs "
                "(contract_id, evidence_snapshot_id, generated_at, model_name, model_version, "
                "input_hash, parameters, probabilities, status) "
                "VALUES (?, ?, ?, 'crypto', 'v1', 'input', '{}', '{}', 'signal_candidate')",
                (contract_id, evidence_id, created),
            )
        connection.execute(
            "INSERT INTO signals "
            "(contract_id, prediction_run_id, evidence_snapshot_id, side, outcome_label, "
            "token_id, generated_at, model_probability, executable_ask, raw_edge, "
            "usable_edge, buffers, fingerprint, freshness_seconds, signal_data) VALUES "
            "(?, ?, ?, 'buy', 'YES', 'yes-token', ?, 0.8, 0.4, 0.4, 0.3, '{}', "
            "'signal-fp', 1, '{}')",
            (contract_id, prediction_id, evidence_id, created),
        )

    command.downgrade(config, "0006")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM prediction_runs").fetchone() == (1,)
