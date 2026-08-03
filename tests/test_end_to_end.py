import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import (
    Base,
    Event,
    ExecutionControlState,
    ForecastMember,
    ForecastRun,
    Market,
    NormalizedRule,
    OrderBookSnapshot,
    PaperPosition,
    ProbabilityEstimate,
    RejectedSignal,
    Signal,
)
from app.schemas import ForecastResult, GammaEvent, OrderBook, OrderLevel, PaperAlert, Station
from app.services.forecast import parse_open_meteo_response
from app.services.polymarket import parse_gamma_search
from app.services.rules import load_station_registry
from app.worker import scan_once

FIXTURES = Path(__file__).parent / "fixtures"


class StaticPolymarket:
    def __init__(self, event: GammaEvent, books: tuple[OrderBook, ...]) -> None:
        self.event = event
        self.books = books

    async def discover_temperature_events(self) -> tuple[GammaEvent, ...]:
        return (self.event,)

    async def get_order_books(self, token_ids: Sequence[str]) -> tuple[OrderBook, ...]:
        return tuple(book for book in self.books if book.asset_id in token_ids)


class StaticForecast:
    def __init__(self, result: ForecastResult) -> None:
        self.result = result

    async def get_forecast(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        timezone: str,
    ) -> ForecastResult:
        return self.result


class RecordingTelegram:
    def __init__(self) -> None:
        self.alerts: list[PaperAlert] = []

    async def send_signal(self, alert: PaperAlert) -> bool:
        self.alerts.append(alert)
        return True


def book(condition: str, asset: str, ask: str) -> OrderBook:
    ask_decimal = Decimal(ask)
    bid = ask_decimal - Decimal("0.02")
    return OrderBook(
        condition_id=condition,
        asset_id=asset,
        timestamp="1785672000000",
        bids=(OrderLevel(price=bid, size=Decimal("100")),),
        asks=(OrderLevel(price=ask_decimal, size=Decimal("100")),),
        best_bid=bid,
        best_ask=ask_decimal,
        spread=Decimal("0.02"),
        midpoint=(ask_decimal + bid) / 2,
        available_depth=Decimal("100"),
        minimum_order_size=Decimal("5"),
        tick_size=Decimal("0.01"),
        raw_data={},
    )


@pytest.mark.asyncio
async def test_recorded_london_market_runs_end_to_end_without_duplicate_alerts(
    tmp_path: Path,
) -> None:
    gamma_payload = json.loads((FIXTURES / "london_event.json").read_text(encoding="utf-8"))
    event = parse_gamma_search(gamma_payload)[0]
    ensemble_payload = json.loads((FIXTURES / "london_ensemble.json").read_text(encoding="utf-8"))
    retrieved_at = datetime(2026, 8, 2, 9, tzinfo=UTC)
    forecast = parse_open_meteo_response(
        ensemble_payload, model="gfs_seamless", retrieved_at=retrieved_at
    )
    books = (
        book("condition-low", "yes-low", "0.80"),
        book("condition-exact", "yes-exact", "0.10"),
        book("condition-high", "yes-high", "0.80"),
    )
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'end-to-end.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        min_ensemble_members=2,
        min_usable_edge=Decimal("0.10"),
        estimated_fee=Decimal("0.00"),
        slippage_buffer=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.04"),
        rule_risk_buffer=Decimal("0.02"),
        paper_starting_balance=Decimal("100"),
    )
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
                request_id="weather-test",
                actor="test",
                reason="test entries allowed",
                updated_at=retrieved_at,
            )
        )
        await session.commit()
    telegram = RecordingTelegram()
    stations: dict[str, Station] = load_station_registry(Path("config/stations.yaml"))

    for _ in range(2):
        await scan_once(
            settings=settings,
            sessions=sessions,
            polymarket=StaticPolymarket(event, books),
            forecast_providers=(StaticForecast(forecast),),
            stations=stations,
            overrides={"775541": {"rounding_method": "half_up"}},
            telegram=telegram,
            now=retrieved_at,
        )

    async with sessions() as session:
        counts = {}
        for model in (
            Event,
            Market,
            OrderBookSnapshot,
            NormalizedRule,
            ForecastRun,
            ForecastMember,
            ProbabilityEstimate,
            Signal,
            RejectedSignal,
            PaperPosition,
        ):
            counts[model.__tablename__] = await session.scalar(
                select(func.count()).select_from(model)
            )

    assert counts["events"] == 1
    assert counts["markets"] == 3
    assert counts["order_book_snapshots"] == 6
    assert counts["normalized_rules"] == 3
    assert counts["forecast_runs"] == 6
    assert counts["forecast_members"] == 18
    assert counts["probability_estimates"] == 6
    assert counts["signals"] == 1
    assert counts["paper_positions"] == 1
    assert counts["rejected_signals"] == 5
    assert len(telegram.alerts) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_rejects_incomplete_domain_contract_before_forecast(
    tmp_path: Path,
) -> None:
    gamma_payload = json.loads((FIXTURES / "london_event.json").read_text(encoding="utf-8"))
    event = parse_gamma_search(gamma_payload)[0].model_copy(update={"end_date": None})
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'worker-strict.db').as_posix()}"
    settings = Settings(database_url=database_url)
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, ()),
        forecast_providers=(),
        stations=load_station_registry(Path("config/stations.yaml")),
        overrides={"775541": {"rounding_method": "half_up"}},
        telegram=None,
        now=datetime(2026, 8, 2, 9, tzinfo=UTC),
    )

    async with sessions() as session:
        rejections = (await session.scalars(select(RejectedSignal))).all()
        forecast_count = await session.scalar(select(func.count()).select_from(ForecastRun))
    assert len(rejections) == 3
    assert all(
        rejection.reasons == ["weather_contract_invalid:expiry_missing"] for rejection in rejections
    )
    assert forecast_count == 0
    await engine.dispose()
