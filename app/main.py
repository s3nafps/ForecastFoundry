import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI, Request
from sqlalchemy import select, text

from app.api import router as api_router
from app.config import Settings
from app.dashboard import router as dashboard_router
from app.database import make_engine, make_session_factory
from app.logging import configure_logging
from app.models import ApplicationSetting, Outcome, ProviderError
from app.schemas import OrderBook
from app.services.forecast import OpenMeteoProvider
from app.services.http import CircuitBreaker, ResilientHttpClient
from app.services.polymarket import PolymarketClient
from app.services.rules import load_market_overrides, load_station_registry
from app.services.settlement import settle_resolved_markets
from app.services.telegram import TelegramClient
from app.services.websocket import MarketWebSocket
from app.worker import maybe_alert_provider_errors, order_book_snapshot, scan_once


def create_app(settings: Settings | None = None) -> FastAPI:
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
        telegram = None
        if resolved.telegram_bot_token and resolved.telegram_admin_user_id:
            telegram = TelegramClient(
                provider_http,
                token=resolved.telegram_bot_token.get_secret_value(),
                chat_id=resolved.telegram_admin_user_id,
            )

        app.state.settings = resolved
        app.state.sessions = sessions

        async def scheduled_scan() -> str:
            async with sessions() as session:
                paused = await session.get(ApplicationSetting, "paused")
            if paused and paused.value is True:
                return "paused"
            await scan_once(
                settings=resolved,
                sessions=sessions,
                polymarket=polymarket,
                forecast_providers=forecasts,
                stations=stations,
                overrides=overrides,
                telegram=telegram,
                now=datetime.now(UTC),
            )
            if resolved.settlement_enabled:
                await settle_resolved_markets(
                    sessions=sessions,
                    polymarket=polymarket,
                    starting_balance=resolved.paper_starting_balance,
                    now=datetime.now(UTC),
                )
            if telegram:
                await maybe_alert_provider_errors(
                    sessions=sessions,
                    telegram=telegram,
                    threshold=resolved.provider_error_alert_threshold,
                    now=datetime.now(UTC),
                )
            return "completed"

        async def store_websocket_book(book: OrderBook) -> None:
            async with sessions() as session:
                outcome = await session.scalar(
                    select(Outcome).where(Outcome.token_id == book.asset_id)
                )
                if outcome is None:
                    return
                session.add(
                    order_book_snapshot(
                        market_id=outcome.market_id,
                        outcome_id=outcome.id,
                        book=book,
                        captured_at=datetime.now(UTC),
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

        scheduler: AsyncIOScheduler | None = None
        websocket_task: asyncio.Task[None] | None = None
        if resolved.app_env != "test":
            scheduler = AsyncIOScheduler(timezone="UTC")
            scheduler.add_job(
                scheduled_scan,
                "interval",
                seconds=min(resolved.polymarket_poll_seconds, resolved.weather_poll_seconds),
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

    application = FastAPI(title="WeatherEdge", version="0.1.0", lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "PAPER_ONLY"}

    @application.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        async with request.app.state.sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}

    if resolved.app_env == "test":

        @application.post("/internal/scan")
        async def internal_scan(request: Request) -> dict[str, str]:
            return {"status": await request.app.state.run_scan()}

    application.include_router(api_router)
    application.include_router(dashboard_router)
    return application


app = create_app()
