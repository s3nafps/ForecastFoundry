import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli import main as cli_main
from app.config import Settings
from app.database import make_engine, make_session_factory
from app.domains.base import MarketInput
from app.mcp_server import create_server, invoke_tool
from app.models import Base, DomainContract
from app.services.application import ApplicationServices
from app.services.crypto_data import CryptoCandle, CryptoSeries, normalize_candles
from app.services.crypto_pipeline import CryptoPaperPipeline


def unresolved_weather(market_id: str = "weather-unresolved") -> MarketInput:
    return MarketInput(
        market_id=market_id,
        title="Will London reach 25°C?",
        description="temperature",
    )


async def services_for(tmp_path: Path) -> tuple[ApplicationServices, AsyncEngine]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'contracts.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    services = ApplicationServices(
        make_session_factory(engine), Settings(app_env="test", database_url=database_url)
    )
    return services, engine


@pytest.mark.asyncio
async def test_application_persists_rejection_reasons_idempotently(tmp_path: Path) -> None:
    services, engine = await services_for(tmp_path)

    first = await services.scan_markets([unresolved_weather()])
    second = await services.scan_markets([unresolved_weather()])

    assert first["markets"][0]["accepted"] is False
    reasons = first["markets"][0]["reasons"]
    assert second["markets"][0]["reasons"] == reasons
    assert isinstance(reasons, list)
    assert str(reasons[0]).startswith("weather_contract_invalid:")
    async with services.sessions() as session:
        rows = (await session.scalars(select(DomainContract))).all()
    assert len(rows) == 1
    assert rows[0].accepted is False
    assert rows[0].rejection_reasons == reasons
    await engine.dispose()


class StaticCryptoData:
    async def fetch_series(
        self, source: str, *, asset: str, quote: str, now: datetime
    ) -> CryptoSeries:
        rows = tuple(
            CryptoCandle(now - timedelta(hours=hours), Decimal(price))
            for hours, price in ((2, "100"), (1, "101"), (0, "102"))
        )
        return normalize_candles(source, rows, now=now)


@pytest.mark.asyncio
async def test_crypto_pipeline_persists_rejected_contract_idempotently(tmp_path: Path) -> None:
    services, engine = await services_for(tmp_path)
    pipeline = CryptoPaperPipeline(services.sessions, StaticCryptoData())  # type: ignore[arg-type]
    market = MarketInput(
        market_id="btc-rejected",
        title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
        description="BTC-USD close",
    )

    first = await pipeline.run(market)
    second = await pipeline.run(market)

    assert first == second
    assert first["status"] == "rejected"
    async with services.sessions() as session:
        rows = (await session.scalars(select(DomainContract))).all()
    assert len(rows) == 1
    assert rows[0].accepted is False
    assert rows[0].rejection_reasons == first["reasons"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_crypto_pipeline_persists_pipeline_rejection_reason(tmp_path: Path) -> None:
    services, engine = await services_for(tmp_path)
    pipeline = CryptoPaperPipeline(services.sessions, StaticCryptoData())  # type: ignore[arg-type]
    market = MarketInput(
        market_id="btc-unsupported-source",
        title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
        description="Chainlink BTC-USD closing price, rounded to nearest dollar.",
    )

    result = await pipeline.run(market)

    assert result["reasons"] == ["unsupported_public_source"]
    async with services.sessions() as session:
        row = await session.scalar(select(DomainContract))
    assert row is not None
    assert row.accepted is False
    assert row.rejection_reasons == result["reasons"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_acceptance_then_pipeline_rejection_updates_same_contract(
    tmp_path: Path,
) -> None:
    services, engine = await services_for(tmp_path)
    pipeline = CryptoPaperPipeline(services.sessions, StaticCryptoData())  # type: ignore[arg-type]
    market = MarketInput(
        market_id="btc-adjudicated",
        title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
        description="Chainlink BTC-USD closing price, rounded to nearest dollar.",
    )

    application_result = await services.scan_markets([market])
    pipeline_result = await pipeline.run(market)

    assert application_result["markets"][0]["accepted"] is True
    assert pipeline_result["reasons"] == ["unsupported_public_source"]
    async with services.sessions() as session:
        rows = (await session.scalars(select(DomainContract))).all()
    assert len(rows) == 1
    assert rows[0].accepted is False
    assert rows[0].rejection_reasons == ["unsupported_public_source"]
    assert rows[0].contract_data["resolution_source"] == "chainlink"
    await engine.dispose()


@pytest.mark.asyncio
async def test_mutable_raw_market_fields_do_not_change_contract_identity(tmp_path: Path) -> None:
    services, engine = await services_for(tmp_path)
    base = {
        "market_id": "btc-mutable",
        "title": "Will BTC be above $100 at 2026-09-01 00:00 UTC?",
        "description": "Coinbase BTC-USD closing price, rounded to nearest dollar.",
    }

    await services.scan_markets(
        [MarketInput(**base, raw_data={"active": True, "liquidity": "100"})]
    )
    await services.scan_markets(
        [MarketInput(**base, raw_data={"active": False, "liquidity": "250"})]
    )

    async with services.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(DomainContract))
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_distinct_market_ids_have_distinct_contract_identity(tmp_path: Path) -> None:
    services, engine = await services_for(tmp_path)
    markets = [
        MarketInput(
            market_id=market_id,
            title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
            description="Coinbase BTC-USD closing price, rounded to nearest dollar.",
        )
        for market_id in ("btc-one", "btc-two")
    ]

    await services.scan_markets(markets)

    async with services.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(DomainContract))
    assert count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_contract_retries_share_one_row(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent-contract.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    first_reads = asyncio.Barrier(2)

    class SynchronizedSession(AsyncSession):
        first_contract_read = True

        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            result = await super().scalar(*args, **kwargs)
            if self.first_contract_read:
                self.first_contract_read = False
                await first_reads.wait()
            return result

    sessions = async_sessionmaker(engine, class_=SynchronizedSession, expire_on_commit=False)
    services = ApplicationServices(sessions, Settings(app_env="test", database_url=database_url))

    first, second = await asyncio.gather(
        services.scan_markets([unresolved_weather()]),
        services.scan_markets([unresolved_weather()]),
    )

    assert first["markets"][0]["reasons"] == second["markets"][0]["reasons"]
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        count = await session.scalar(select(func.count()).select_from(DomainContract))
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_and_crypto_pipeline_share_one_contract_fingerprint(
    tmp_path: Path,
) -> None:
    services, engine = await services_for(tmp_path)
    market = MarketInput(
        market_id="btc-shared",
        title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
        description="Coinbase BTC-USD closing price, rounded to nearest dollar.",
    )
    pipeline = CryptoPaperPipeline(services.sessions, StaticCryptoData())  # type: ignore[arg-type]

    assert (await services.scan_markets([market]))["markets"][0]["accepted"] is True
    pipeline_result = await pipeline.run(market, now=datetime(2026, 8, 3, 12, tzinfo=UTC))
    assert pipeline_result["status"] == "rejected"
    assert pipeline_result["reasons"] == ["market_metadata_missing"]

    async with services.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(DomainContract))
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_scan_fails_closed_through_shared_application_service(tmp_path: Path) -> None:
    services, engine = await services_for(tmp_path)
    server = create_server(services)

    result = await invoke_tool(
        server, "scan_markets", {"markets": [unresolved_weather().model_dump(mode="json")]}
    )

    assert result["markets"][0]["accepted"] is False
    await engine.dispose()


def test_cli_scan_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cli-contract.db'}")

    assert (
        cli_main(
            [
                "scan",
                "--market-id",
                "cli-weather",
                "--title",
                "Will London reach 25°C?",
                "--description",
                "temperature",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["markets"][0]["accepted"] is False
