import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, text

from app import PRODUCT_NAME
from app.api import router as api_router
from app.config import Settings
from app.dashboard import router as dashboard_router
from app.database import make_engine, make_session_factory
from app.domains.base import MarketInput
from app.logging import configure_logging
from app.models import DomainContract, OrderBookSnapshot, Outcome, ProviderError
from app.observability import Metrics
from app.providers.registry import ProviderRegistry
from app.schemas import OrderBook
from app.services.application import ApplicationServices
from app.services.crypto_data import CryptoMarketDataClient
from app.services.crypto_pipeline import CryptoPaperPipeline
from app.services.execution_control import ExecutionControl
from app.services.forecast import OpenMeteoProvider
from app.services.http import CircuitBreaker, ResilientHttpClient
from app.services.observations import AviationWeatherObservations, ingest_observations
from app.services.paper import SettlementFetcher
from app.services.polymarket import PolymarketClient
from app.services.provider_health import ProviderHealthMonitor
from app.services.rules import load_market_overrides, load_station_registry
from app.services.settlement import ProductionSettlementFetcher
from app.services.telegram import TelegramClient
from app.services.websocket import MarketWebSocket
from app.worker import scan_once


def create_app(
    settings: Settings | None = None,
    *,
    settlement_fetcher: SettlementFetcher | None = None,
) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resolved.app_env != "test":
            configure_logging(
                resolved.log_level,
                secrets=(
                    resolved.telegram_bot_token.get_secret_value()
                    if resolved.telegram_bot_token
                    else "",
                ),
            )
        engine = make_engine(resolved.database_url)
        sessions = make_session_factory(engine)
        raw_http = httpx.AsyncClient(timeout=resolved.http_timeout_seconds)
        provider_http = ResilientHttpClient(
            raw_http,
            max_retries=resolved.http_max_retries,
            circuit_breaker=CircuitBreaker(
                failure_threshold=resolved.provider_circuit_failures,
                reset_seconds=resolved.provider_circuit_reset_seconds,
            ),
        )
        polymarket = PolymarketClient(provider_http, resolved.gamma_api_url, resolved.clob_api_url)
        forecasts = tuple(
            OpenMeteoProvider(provider_http, resolved.open_meteo_api_url, model.strip())
            for model in resolved.forecast_models.split(",")
            if model.strip()
        )
        stations = load_station_registry(Path(resolved.station_config_path))
        overrides = load_market_overrides(Path(resolved.market_overrides_path))
        aviation_weather = AviationWeatherObservations(
            provider_http, resolved.aviation_weather_api_url
        )
        telegram = None
        if resolved.telegram_bot_token and resolved.telegram_admin_user_id:
            telegram = TelegramClient(
                provider_http,
                token=resolved.telegram_bot_token.get_secret_value(),
                chat_id=resolved.telegram_admin_user_id,
            )

        app.state.settings = resolved
        app.state.sessions = sessions
        app.state.metrics = Metrics()
        crypto_data = CryptoMarketDataClient(provider_http)
        active_settlement_fetcher = settlement_fetcher or ProductionSettlementFetcher(
            sessions, crypto_data
        )
        providers = (
            ProviderHealthMonitor(sessions, ProviderRegistry.default(), provider_http)
            if resolved.app_env != "test"
            else None
        )
        app.state.services = ApplicationServices(
            sessions,
            resolved,
            crypto_pipeline=CryptoPaperPipeline(
                sessions,
                crypto_data,
                pricing=polymarket,
                settings=resolved,
            ),
            health_monitor=providers,
        )

        async def scheduled_scan() -> str:
            services = cast(ApplicationServices, app.state.services)
            return await services.run_scheduled_scan(
                lambda: scan_once(
                    settings=resolved,
                    sessions=sessions,
                    polymarket=polymarket,
                    forecast_providers=forecasts,
                    stations=stations,
                    overrides=overrides,
                    telegram=telegram,
                    now=datetime.now(UTC),
                )
            )

        async def scheduled_crypto_scan() -> str:
            async with sessions() as session:
                if (await ExecutionControl(session).snapshot()).paused:
                    return "paused"
            events = await polymarket.discover_crypto_events()
            markets = tuple(
                MarketInput(
                    market_id=market.id,
                    title=market.question,
                    description=market.description,
                    raw_data={
                        "event": event.model_dump(mode="json"),
                        "market": market.model_dump(mode="json"),
                    },
                )
                for event in events
                for market in event.markets
            )
            await app.state.services.scan_markets(markets, now=datetime.now(UTC))
            return "completed"

        async def scheduled_settlement() -> dict[str, object]:
            services = cast(ApplicationServices, app.state.services)
            return await services.run_settlement_job(
                active_settlement_fetcher, now=datetime.now(UTC)
            )

        async def scheduled_observation_ingest() -> dict[str, object]:
            stations_by_id = {station.station_id: station for station in stations.values()}
            async with sessions() as session:
                contracts = (
                    await session.scalars(
                        select(DomainContract).where(DomainContract.domain == "weather")
                    )
                ).all()
            ingested_total = 0
            errors = 0
            for contract in contracts:
                if contract.expiry is None:
                    continue
                now = datetime.now(UTC)
                if contract.expiry - now > timedelta(hours=resolved.observation_blend_hours):
                    continue
                source = str(contract.resolution_source)
                data = contract.contract_data
                station_id = str(data.get("station_id", ""))
                raw_local_date = data.get("local_date")
                raw_timezone = data.get("timezone")
                if (
                    station_id not in stations_by_id
                    or not isinstance(raw_local_date, str)
                    or not isinstance(raw_timezone, str)
                ):
                    continue
                try:
                    local_date = datetime.fromisoformat(raw_local_date).date()
                    window_end = datetime.combine(
                        local_date + timedelta(days=1), time.min, ZoneInfo(raw_timezone)
                    ).astimezone(UTC)
                    # Keep ingesting until the local reporting window is complete
                    # (next local midnight); settlement coverage requires the final
                    # hours of the local day, which can fall after contract expiry.
                    if now > window_end + timedelta(hours=1):
                        continue
                    rows = await aviation_weather.fetch(station_id, local_date)
                except Exception as exc:
                    errors += 1
                    async with sessions() as session:
                        session.add(
                            ProviderError(
                                provider="aviation_weather",
                                operation="observe",
                                error_type=type(exc).__name__,
                                message=str(exc),
                                details={},
                                retryable=True,
                                occurred_at=datetime.now(UTC),
                            )
                        )
                        await session.commit()
                    continue
                if not rows:
                    continue
                async with sessions() as session:
                    ingested_total += await ingest_observations(
                        session,
                        station_id=station_id,
                        source=source,
                        rows=rows,
                        retrieved_at=datetime.now(UTC),
                    )
                    await session.commit()
            return {"status": "completed", "ingested": ingested_total, "errors": errors}

        async def store_websocket_book(book: OrderBook) -> None:
            async with sessions() as session:
                outcome = await session.scalar(
                    select(Outcome).where(Outcome.token_id == book.asset_id)
                )
                if outcome is None:
                    return
                session.add(
                    OrderBookSnapshot(
                        market_id=outcome.market_id,
                        outcome_id=outcome.id,
                        captured_at=datetime.now(UTC),
                        bids=[level.model_dump(mode="json") for level in book.bids],
                        asks=[level.model_dump(mode="json") for level in book.asks],
                        best_bid=book.best_bid,
                        best_ask=book.best_ask,
                        spread=book.spread,
                        midpoint=book.midpoint,
                        available_depth=book.available_depth,
                        minimum_order_size=book.minimum_order_size,
                        tick_size=book.tick_size,
                        raw_data=book.raw_data,
                    )
                )
                await session.commit()

        async def websocket_listener() -> None:
            websocket = MarketWebSocket()
            while True:
                try:
                    events = await polymarket.discover_temperature_events()
                    token_ids = tuple(
                        token_id
                        for event in events
                        for market in event.markets
                        for token_id in market.token_ids
                    )
                    if token_ids:
                        await websocket.run(token_ids, store_websocket_book)
                    else:
                        await asyncio.sleep(resolved.polymarket_poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    async with sessions() as session:
                        session.add(
                            ProviderError(
                                provider="polymarket_websocket",
                                operation="listen",
                                error_type=type(exc).__name__,
                                message=str(exc),
                                details={},
                                retryable=True,
                                occurred_at=datetime.now(UTC),
                            )
                        )
                        await session.commit()
                    await asyncio.sleep(5)

        app.state.run_scan = scheduled_scan
        app.state.run_crypto_scan = scheduled_crypto_scan
        app.state.run_settlement = scheduled_settlement
        app.state.run_observation_ingest = scheduled_observation_ingest

        scheduler: AsyncIOScheduler | None = None
        websocket_task: asyncio.Task[None] | None = None
        if resolved.app_env != "test" and resolved.scheduler_enabled:
            scheduler = AsyncIOScheduler(timezone="UTC")
            scheduler.add_job(
                scheduled_scan,
                "interval",
                seconds=min(resolved.polymarket_poll_seconds, resolved.weather_poll_seconds),
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                scheduled_crypto_scan,
                "interval",
                seconds=min(resolved.polymarket_poll_seconds, resolved.observation_poll_seconds),
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                scheduled_settlement,
                "interval",
                seconds=resolved.observation_poll_seconds,
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                scheduled_observation_ingest,
                "interval",
                seconds=resolved.observation_poll_seconds,
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            if resolved.polymarket_websocket_enabled:
                websocket_task = asyncio.create_task(websocket_listener())
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            if websocket_task:
                websocket_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await websocket_task
            await raw_http.aclose()
            await engine.dispose()

    application = FastAPI(title=PRODUCT_NAME, version="0.1.0", lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "PAPER_ONLY"}

    @application.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        async with request.app.state.sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @application.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> str:
        return cast(Metrics, request.app.state.metrics).render()

    if resolved.app_env == "test":

        @application.post("/internal/scan")
        async def internal_scan(request: Request) -> dict[str, str]:
            return {"status": await request.app.state.run_scan()}

    application.include_router(api_router)
    application.include_router(dashboard_router)
    return application


app = create_app()
