import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import (
    Base,
    Event,
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
from app.services.http import ProviderResponseError
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


class FlakyTelegram(RecordingTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 1

    async def send_signal(self, alert: PaperAlert) -> bool:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ProviderResponseError("telegram unavailable")
        return await super().send_signal(alert)


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


ScanSetup = tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    Settings,
    GammaEvent,
    tuple[OrderBook, ...],
    ForecastResult,
    dict[str, Station],
]


async def scan_setup(tmp_path: Path) -> ScanSetup:
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
    )
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    stations: dict[str, Station] = load_station_registry(Path("config/stations.yaml"))
    return engine, sessions, settings, event, books, forecast, stations


async def table_counts(
    sessions: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
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
    return counts


@pytest.mark.asyncio
async def test_recorded_london_market_runs_end_to_end_without_duplicate_alerts(
    tmp_path: Path,
) -> None:
    engine, sessions, settings, event, books, forecast, stations = await scan_setup(tmp_path)
    telegram = RecordingTelegram()
    retrieved_at = datetime(2026, 8, 2, 9, tzinfo=UTC)

    try:
        for _ in range(2):
            await scan_once(
                settings=settings,
                sessions=sessions,
                polymarket=StaticPolymarket(event, books),
                forecast_providers=(StaticForecast(forecast),),
                stations=stations,
                overrides={},
                telegram=telegram,
                now=retrieved_at,
            )

        counts = await table_counts(sessions)
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
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_telegram_delivery_is_retried_on_the_next_scan(tmp_path: Path) -> None:
    engine, sessions, settings, event, books, forecast, stations = await scan_setup(tmp_path)
    telegram = FlakyTelegram()
    retrieved_at = datetime(2026, 8, 2, 9, tzinfo=UTC)

    try:
        for _ in range(3):
            await scan_once(
                settings=settings,
                sessions=sessions,
                polymarket=StaticPolymarket(event, books),
                forecast_providers=(StaticForecast(forecast),),
                stations=stations,
                overrides={},
                telegram=telegram,
                now=retrieved_at,
            )

        counts = await table_counts(sessions)
        async with sessions() as session:
            signal = await session.scalar(select(Signal))

        assert counts["signals"] == 1
        assert counts["paper_positions"] == 1
        assert len(telegram.alerts) == 1
        assert signal is not None and signal.alerted_at is not None
        assert signal.alert_error is None
    finally:
        await engine.dispose()
