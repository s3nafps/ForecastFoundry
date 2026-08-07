import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.domains.base import MarketInput
from app.domains.weather import WeatherPlugin
from app.models import Base, DomainContract, PaperPosition, PaperSettlement, Signal
from app.services.observations import ObservedHour, ingest_observations
from app.services.paper import PaperLifecycle, SettlementWorker
from app.services.polymarket import parse_gamma_search
from app.services.rules import load_station_registry
from app.services.settlement import ProductionSettlementFetcher

SOURCE = "https://aviationweather.gov/api/data/metar?ids=EGLC&format=json"
OVERRIDES = {
    "775541": {
        "resolution_source": SOURCE,
        "station_id": "EGLC",
        "rounding_method": "half_up",
    }
}


class _UnusedCrypto:
    """The weather settlement branch never touches the crypto client."""


def _london_contract(*, market_external_id: str, fingerprint: str) -> DomainContract:
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "london_event.json").read_text(encoding="utf-8")
    )
    event = parse_gamma_search(raw)[0]
    route = WeatherPlugin(
        stations=load_station_registry(Path("config/stations.yaml")),
        overrides=OVERRIDES,
    ).normalize(
        MarketInput(
            market_id=market_external_id,
            title=event.title,
            description=event.description,
            raw_data={"event": event.model_dump(mode="json")},
        )
    )
    assert route.accepted is True
    assert route.contract is not None
    return DomainContract(
        market_external_id=market_external_id,
        domain="weather",
        accepted=True,
        resolution_source=SOURCE,
        expiry=route.contract.expiry,
        contract_data=route.contract.model_dump(mode="json"),
        rejection_reasons=[],
        provenance=route.contract.provenance,
        fingerprint=fingerprint,
    )


def _bucket_label(
    contract: DomainContract, *, lower: int | None = None, upper: int | None = None
) -> str:
    raw_buckets = contract.contract_data["buckets"]
    if not isinstance(raw_buckets, list):
        raise AssertionError("contract buckets must be a list")
    for raw in raw_buckets:
        bucket = cast(dict[str, object], raw)
        if lower is not None and bucket.get("lower") == lower:
            return str(bucket["label"])
        if upper is not None and bucket.get("upper") == upper:
            return str(bucket["label"])
    raise AssertionError("bucket not found in contract")


def _daily_max_hours(local_start: datetime) -> tuple[ObservedHour, ...]:
    return tuple(
        ObservedHour(
            station_id="EGLC",
            observed_at=local_start + timedelta(hours=3 * index),
            temperature_celsius=24.6 if index == 4 else 20 + index / 10,
            raw_ob=f"EGLC flow {index}",
            quality_flags=(),
        )
        for index in range(8)
    )


async def _seed_flow(
    sessions: async_sessionmaker[AsyncSession], *, win_label: str, lose_label: str
) -> tuple[int, int]:
    win_contract = _london_contract(market_external_id="3237364", fingerprint="flow-win-contract")
    lose_contract = _london_contract(market_external_id="3237362", fingerprint="flow-lose-contract")
    assert win_contract.expiry is not None
    assert lose_contract.expiry is not None
    local_start = datetime(2026, 8, 1, 23, 30, tzinfo=UTC)
    async with sessions() as session:
        await ingest_observations(
            session,
            station_id="EGLC",
            source=SOURCE,
            rows=_daily_max_hours(local_start),
            retrieved_at=datetime.now(UTC),
        )
        session.add_all((win_contract, lose_contract))
        await session.flush()
        win_signal = Signal(
            contract_id=win_contract.id,
            outcome_label=win_label,
            generated_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            model_probability=Decimal("0.55"),
            executable_ask=Decimal("0.55"),
            raw_edge=Decimal("0.15"),
            usable_edge=Decimal("0.10"),
            buffers={},
            fingerprint="flow-win-signal",
            signal_data={},
        )
        lose_signal = Signal(
            contract_id=lose_contract.id,
            outcome_label=lose_label,
            generated_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            model_probability=Decimal("0.45"),
            executable_ask=Decimal("0.45"),
            raw_edge=Decimal("0.10"),
            usable_edge=Decimal("0.05"),
            buffers={},
            fingerprint="flow-lose-signal",
            signal_data={},
        )
        session.add_all((win_signal, lose_signal))
        await session.flush()
        win_position = PaperPosition(
            signal_id=win_signal.id,
            entered_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            entry_price=Decimal("0.55"),
            amount=Decimal("5.00"),
            shares=Decimal("9"),
            fees=Decimal("0.00"),
            status="open",
            current_mark=Decimal("0.55"),
            unrealized_pnl=Decimal("0.00"),
            realized_pnl=Decimal("0.00"),
            signal_data={},
        )
        lose_position = PaperPosition(
            signal_id=lose_signal.id,
            entered_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            entry_price=Decimal("0.45"),
            amount=Decimal("5.00"),
            shares=Decimal("11"),
            fees=Decimal("0.00"),
            status="open",
            current_mark=Decimal("0.45"),
            unrealized_pnl=Decimal("0.00"),
            realized_pnl=Decimal("0.00"),
            signal_data={},
        )
        session.add_all((win_position, lose_position))
        await session.commit()
        return win_position.id, lose_position.id


async def test_weather_fetcher_to_settlement_wins_for_winning_bucket(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'weather-flow.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    win_contract = _london_contract(market_external_id="3237364", fingerprint="flow-win-contract")
    win_label = _bucket_label(win_contract, lower=24)
    lose_label = _bucket_label(win_contract, upper=22)
    assert win_label != lose_label
    sessions = make_session_factory(engine)
    win_position_id, lose_position_id = await _seed_flow(
        sessions, win_label=win_label, lose_label=lose_label
    )
    fetcher = ProductionSettlementFetcher(sessions, _UnusedCrypto())  # type: ignore[arg-type]
    lifecycle = PaperLifecycle(sessions, Settings(app_env="test"))
    results = await SettlementWorker(lifecycle).run_due(fetcher)
    by_position = {row["position_id"]: row for row in results}

    assert by_position[win_position_id]["status"] == "settled"
    assert by_position[win_position_id]["outcome"] == "YES"
    assert by_position[win_position_id]["won"] is True
    assert by_position[win_position_id]["payout"] == "9.00000000"
    assert by_position[lose_position_id]["status"] == "settled"
    assert by_position[lose_position_id]["outcome"] == "NO"
    assert by_position[lose_position_id]["won"] is False
    assert by_position[lose_position_id]["payout"] == "0.00000000"

    async with sessions() as session:
        settlements = (await session.scalars(select(PaperSettlement))).all()
    settled_by_position = {row.position_id: row for row in settlements}
    assert settled_by_position[win_position_id].won is True
    assert settled_by_position[win_position_id].payout == Decimal("9")
    assert (
        settled_by_position[win_position_id].resolution_data["resolved_bucket"] == win_label
    )
    assert settled_by_position[lose_position_id].won is False
    assert settled_by_position[lose_position_id].payout == Decimal("0")
    await engine.dispose()
