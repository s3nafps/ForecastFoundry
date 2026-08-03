from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import make_engine, make_session_factory
from app.models import Base, DomainContract, Observation, Signal
from app.services.crypto_data import CryptoCandle, CryptoSeries, _endpoint
from app.services.settlement import ProductionSettlementFetcher

EXPIRY = datetime(2026, 8, 3, 5, tzinfo=UTC)


def _provider_response(source: str, price: str) -> object:
    opened = int((EXPIRY - timedelta(hours=1)).timestamp())
    if source == "coinbase":
        return [[opened, price, price, price, price, "1"]]
    if source == "binance":
        return [[opened * 1000, price, price, price, price, "1"]]
    return {
        "error": [],
        "result": {
            "XXBTZUSD": [[opened, price, price, price, price, "1", "1", 1]],
            "last": str(opened),
        },
    }


class _CryptoFixture:
    def __init__(self, source: str, response: object, quote: str = "USD") -> None:
        self.source = source
        self.response = response
        self.quote = quote

    async def fetch_series(self, *args: object, **kwargs: object) -> CryptoSeries:
        url, query = _endpoint(self.source, "BTC", self.quote, "1h", 200)
        return CryptoSeries(
            source=self.source,
            candles=(CryptoCandle(EXPIRY - timedelta(hours=1), Decimal("101")),),
            log_returns=(),
            latest_at=EXPIRY,
            interval_seconds=3600,
            retrieved_at=EXPIRY + timedelta(minutes=1),
            provider_version=f"{self.source}-fixture-v1",
            raw_response_hash="unused-series-hash",
            raw_payload=self.response,
            request_url=url,
            request_params=query,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "quote"), (("coinbase", "USD"), ("binance", "USDT"), ("kraken", "USD"))
)
async def test_crypto_fetcher_derives_exact_provider_candle(
    tmp_path: Path, source: str, quote: str
) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / f'{source}.db').as_posix()}")
    sessions = make_session_factory(engine)
    contract = DomainContract(
        id=7,
        market_external_id="btc-resolution",
        domain="crypto",
        accepted=True,
        resolution_source=source,
        expiry=EXPIRY,
        contract_data={"source": source, "asset": "BTC", "quote": quote},
        rejection_reasons=[],
        provenance={},
        fingerprint="crypto-contract",
    )
    fetcher = ProductionSettlementFetcher(
        sessions,
        _CryptoFixture(source, _provider_response(source, "101"), quote),  # type: ignore[arg-type]
    )

    evidence = await fetcher._crypto(contract)

    assert evidence.source == source
    assert evidence.observed_at == EXPIRY
    assert evidence.normalized_values == {
        "asset": "BTC",
        "quote": quote,
        "price": "101",
        "price_definition": "closing price",
        "source_timestamp": EXPIRY.isoformat(),
    }
    assert len(evidence.raw_response_hash) == 64
    await engine.dispose()


@pytest.mark.asyncio
async def test_weather_fetcher_uses_exact_persisted_station_source_and_local_day(
    tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'weather.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    source = "official-station-feed"
    async with sessions() as session:
        session.add_all(
            (
                Observation(
                    market_id=None,
                    station_id="TEST",
                    observed_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
                    air_temperature=24.6,
                    precipitation=None,
                    source=source,
                    retrieved_at=datetime(2026, 8, 3, 0, 1, tzinfo=UTC),
                    quality_flags=[],
                    raw_data={"official": True},
                ),
                Observation(
                    market_id=None,
                    station_id="OTHER",
                    observed_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
                    air_temperature=99,
                    precipitation=None,
                    source=source,
                    retrieved_at=datetime(2026, 8, 3, 0, 1, tzinfo=UTC),
                    quality_flags=[],
                    raw_data={},
                ),
            )
        )
        await session.commit()
    contract = DomainContract(
        id=9,
        market_external_id="weather-resolution",
        domain="weather",
        accepted=True,
        resolution_source=source,
        expiry=datetime(2026, 8, 3, tzinfo=UTC),
        contract_data={
            "station_id": "TEST",
            "timezone": "UTC",
            "local_date": "2026-08-02",
            "measurement": "daily maximum air temperature",
            "rounding_method": "half_up",
            "buckets": [
                {"label": "24 or below", "upper": 24},
                {"label": "25 or above", "lower": 25},
            ],
        },
        rejection_reasons=[],
        provenance={},
        fingerprint="weather-contract",
    )
    signal = Signal(outcome_label="25 or above")
    fetcher = ProductionSettlementFetcher(
        sessions, _CryptoFixture("coinbase", [])  # type: ignore[arg-type]
    )

    evidence = await fetcher._weather(contract, signal)

    assert evidence.normalized_values["rounded_value"] == "25.0"
    assert evidence.normalized_values["bucket_label"] == "25 or above"
    assert len(evidence.raw_payload["observations"]) == 1  # type: ignore[index]
    await engine.dispose()
