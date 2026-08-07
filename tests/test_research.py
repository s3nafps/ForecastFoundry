import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from sqlalchemy import func, select

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base, ExecutionControlState, ProbabilityEstimate, ResearchDocument
from app.schemas import ForecastResult, GammaEvent, OrderBook, OrderLevel
from app.services.forecast import parse_open_meteo_response
from app.services.polymarket import parse_gamma_search
from app.services.research import ingest_github_issues, parse_github_issue
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


def _payload() -> dict[str, object]:
    raw = json.loads((FIXTURES / "github_issues.json").read_text(encoding="utf-8"))
    return cast(dict[str, object], raw)


def test_parse_github_issue_extracts_document_fields() -> None:
    items = _payload()["items"]
    assert isinstance(items, list)
    document = parse_github_issue(items[0], retrieved_at=datetime(2026, 8, 2, 0, 0, tzinfo=UTC))

    assert document.provider == "github"
    assert document.external_id == "1234"
    assert document.url == "https://github.com/open-meteo/open-meteo/issues/1234"
    assert document.feature_only is True
    assert "Ensemble endpoint returns partial data" in document.redacted_text
    assert len(document.content_hash) == 64


async def test_ingest_github_issues_persists_and_deduplicates() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    retrieved_at = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    async with sessions() as session:
        count = await ingest_github_issues(session, _payload(), retrieved_at=retrieved_at)
        assert count == 2
        again = await ingest_github_issues(session, _payload(), retrieved_at=retrieved_at)
        assert again == 0
        rows = (await session.scalars(select(ResearchDocument))).all()
        assert len(rows) == 2
    await engine.dispose()


async def test_research_document_identity_is_unique_at_the_database() -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.services.research import parse_github_issue

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    retrieved_at = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    async with sessions() as session:
        document = parse_github_issue(_payload()["items"][0], retrieved_at=retrieved_at)
        session.add(document)
        await session.commit()
        duplicate = parse_github_issue(_payload()["items"][0], retrieved_at=retrieved_at)
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        remaining = (await session.scalars(select(ResearchDocument))).all()
        assert len(remaining) == 1
    await engine.dispose()


async def test_research_documents_do_not_affect_scan_probabilities(tmp_path: Path) -> None:
    gamma_payload = json.loads((FIXTURES / "london_event.json").read_text(encoding="utf-8"))
    event = parse_gamma_search(gamma_payload)[0]
    ensemble_payload = json.loads((FIXTURES / "london_ensemble.json").read_text(encoding="utf-8"))
    now = datetime(2026, 8, 2, 9, tzinfo=UTC)
    forecast = parse_open_meteo_response(ensemble_payload, model="gfs_seamless", retrieved_at=now)
    books = (
        book("condition-low", "yes-low", "0.80"),
        book("condition-exact", "yes-exact", "0.10"),
        book("condition-high", "yes-high", "0.80"),
    )
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'research-isolation.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        min_ensemble_members=2,
        min_usable_edge=Decimal("0.10"),
        estimated_fee=Decimal("0.00"),
        slippage_buffer=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.04"),
        rule_risk_buffer=Decimal("0.02"),
        paper_starting_balance=Decimal("100"),
        observation_blend_hours=2,
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
                updated_at=now,
            )
        )
        await session.commit()

    async def run_scan() -> None:
        await scan_once(
            settings=settings,
            sessions=sessions,
            polymarket=StaticPolymarket(event, books),
            forecast_providers=(StaticForecast(forecast),),
            stations=load_station_registry(Path("config/stations.yaml")),
            overrides={"775541": {"rounding_method": "half_up"}},
            telegram=None,
            now=now,
        )

    await run_scan()
    async with sessions() as session:
        await ingest_github_issues(session, _payload(), retrieved_at=now)
    await run_scan()

    async with sessions() as session:
        estimates = (
            await session.scalars(
                select(ProbabilityEstimate).order_by(
                    ProbabilityEstimate.generated_at.desc(), ProbabilityEstimate.id.desc()
                )
            )
        ).all()
        research_count = await session.scalar(select(func.count()).select_from(ResearchDocument))

    assert research_count == 2
    assert len(estimates) == 6
    baseline = {estimate.market_id: estimate for estimate in estimates[3:]}
    latest = {estimate.market_id: estimate for estimate in estimates[:3]}
    assert set(latest) == set(baseline)
    for market_id in baseline:
        assert latest[market_id].outcome_probabilities == baseline[market_id].outcome_probabilities
    await engine.dispose()
