import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base, ProbabilityEstimate, RejectedSignal
from app.schemas import ForecastResult, GammaEvent, OrderBook, OrderLevel
from app.services.forecast import parse_open_meteo_response
from app.services.observations import (
    ObservedHour,
    ingest_observations,
)
from app.services.polymarket import parse_gamma_search
from app.services.rules import load_station_registry
from app.worker import scan_once

FIXTURES = Path(__file__).parent / "fixtures"


def _settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        min_usable_edge=Decimal("0.01"),
        min_liquidity_usd=Decimal("0"),
        min_rule_confidence=0,
        observation_blend_hours=48,
        observation_min_count=1,
    )


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
        self, latitude: float, longitude: float, start_date: date, end_date: date, timezone: str
    ) -> ForecastResult:
        return self.result


def _load_event() -> GammaEvent:
    raw = json.loads((FIXTURES / "london_event.json").read_text(encoding="utf-8"))
    return parse_gamma_search(raw)[0]


def _load_forecast() -> ForecastResult:
    raw = json.loads((FIXTURES / "london_ensemble.json").read_text(encoding="utf-8"))
    return parse_open_meteo_response(raw, model="gfs_seamless", retrieved_at=datetime.now(UTC))


def _book(condition: str, asset: str) -> OrderBook:
    return OrderBook(
        condition_id=condition,
        asset_id=asset,
        timestamp="1785672000000",
        bids=(OrderLevel(price=Decimal("0.45"), size=Decimal("100")),),
        asks=(OrderLevel(price=Decimal("0.50"), size=Decimal("100")),),
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.50"),
        spread=Decimal("0.05"),
        midpoint=Decimal("0.475"),
        available_depth=Decimal("100"),
        minimum_order_size=Decimal("5"),
        tick_size=Decimal("0.01"),
        raw_data={},
    )


async def test_observation_blend_changes_probabilities_and_persists_usage() -> None:
    event = _load_event()
    market = event.markets[0]
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    settings = _settings()
    stations = load_station_registry(Path("config/stations.yaml"))
    assert event.end_date is not None
    now = event.end_date - timedelta(hours=6)

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, (_book(market.condition_id, market.token_ids[0]),)),
        forecast_providers=(StaticForecast(_load_forecast()),),
        stations=stations,
        overrides={"775541": {"rounding_method": "half_up"}},
        telegram=None,
        now=now,
    )

    async with sessions() as session:
        baseline = await session.scalar(
            select(ProbabilityEstimate).order_by(
                ProbabilityEstimate.generated_at.desc(), ProbabilityEstimate.id.desc()
            )
        )
        assert baseline is not None
        assert baseline.blend_applied is False
        assert baseline.observations_used == 0
        baseline_probs = dict(baseline.outcome_probabilities)

        await ingest_observations(
            session,
            station_id="EGLC",
            source=str(event.resolution_source),
            rows=(
                ObservedHour(
                    station_id="EGLC",
                    observed_at=now - timedelta(hours=2),
                    temperature_celsius=100.0,
                    raw_ob="EGLC hot",
                    quality_flags=(),
                ),
            ),
            retrieved_at=now,
        )
        await session.commit()

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, (_book(market.condition_id, market.token_ids[0]),)),
        forecast_providers=(StaticForecast(_load_forecast()),),
        stations=stations,
        overrides={"775541": {"rounding_method": "half_up"}},
        telegram=None,
        now=now,
    )

    async with sessions() as session:
        blended = await session.scalar(
            select(ProbabilityEstimate).order_by(
                ProbabilityEstimate.generated_at.desc(), ProbabilityEstimate.id.desc()
            )
        )
        assert blended is not None
        assert blended.blend_applied is True
        assert blended.observations_used >= 1
        assert dict(blended.outcome_probabilities) != baseline_probs
    await engine.dispose()


async def test_missing_observations_inside_blend_horizon_reject_with_stale() -> None:
    event = _load_event()
    market = event.markets[0]
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    settings = _settings()
    stations = load_station_registry(Path("config/stations.yaml"))
    assert event.end_date is not None
    now = event.end_date - timedelta(hours=24)

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, (_book(market.condition_id, market.token_ids[0]),)),
        forecast_providers=(StaticForecast(_load_forecast()),),
        stations=stations,
        overrides={"775541": {"rounding_method": "half_up"}},
        telegram=None,
        now=now,
    )

    async with sessions() as session:
        rejections = (await session.scalars(select(RejectedSignal))).all()
        assert any("observations_stale" in row.reasons for row in rejections)
    await engine.dispose()
