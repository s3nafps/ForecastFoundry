from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app import COMPATIBILITY_VERSION, PRODUCT_NAME
from app.models import (
    ApplicationSetting,
    Event,
    ForecastMember,
    ForecastRun,
    Market,
    NormalizedRule,
    OrderBookSnapshot,
    PaperPosition,
    PaperSettlement,
    ProbabilityEstimate,
    ProviderError,
    RejectedSignal,
    Signal,
)

router = APIRouter(prefix="/api/v1")


async def market_rows(request: Request) -> list[dict[str, object]]:
    async with request.app.state.sessions() as session:
        rows = (
            await session.execute(
                select(Market, Event).join(Event, Event.id == Market.event_id).order_by(Market.id)
            )
        ).all()
    return [
        {
            "id": market.id,
            "polymarket_id": market.polymarket_id,
            "event": event.title,
            "question": market.question,
            "active": market.active,
            "closed": market.closed,
            "close_time": market.close_time,
            "liquidity": market.liquidity,
        }
        for market, event in rows
    ]


async def signal_rows(request: Request) -> list[dict[str, object]]:
    async with request.app.state.sessions() as session:
        rows = (await session.scalars(select(Signal).order_by(Signal.generated_at.desc()))).all()
    return [
        {
            "id": row.id,
            "market_id": row.market_id,
            "generated_at": row.generated_at,
            "model_probability": row.model_probability,
            "executable_ask": row.executable_ask,
            "raw_edge": row.raw_edge,
            "usable_edge": row.usable_edge,
            "alerted_at": row.alerted_at,
        }
        for row in rows
    ]


async def position_rows(request: Request) -> list[dict[str, object]]:
    async with request.app.state.sessions() as session:
        rows = (
            await session.scalars(select(PaperPosition).order_by(PaperPosition.entered_at.desc()))
        ).all()
    return [
        {
            "id": row.id,
            "market_id": row.market_id,
            "entered_at": row.entered_at,
            "entry_price": row.entry_price,
            "amount": row.amount,
            "shares": row.shares,
            "fees": row.fees,
            "status": row.status,
        }
        for row in rows
    ]


async def error_rows(request: Request) -> list[dict[str, object]]:
    async with request.app.state.sessions() as session:
        rows = (
            await session.scalars(select(ProviderError).order_by(ProviderError.occurred_at.desc()))
        ).all()
    return [
        {
            "id": row.id,
            "provider": row.provider,
            "operation": row.operation,
            "error_type": row.error_type,
            "message": row.message,
            "retryable": row.retryable,
            "occurred_at": row.occurred_at,
        }
        for row in rows
    ]


async def performance_data(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    async with request.app.state.sessions() as session:
        balance = await session.get(ApplicationSetting, "paper_balance")
        positions = (await session.scalars(select(PaperPosition))).all()
        settlements = (await session.scalars(select(PaperSettlement))).all()
    realized = sum((row.realized_pnl for row in settlements), Decimal("0"))
    return {
        "starting_balance": str(settings.paper_starting_balance),
        "current_balance": str(balance.value if balance else settings.paper_starting_balance),
        "positions": len(positions),
        "open_positions": sum(row.status == "open" for row in positions),
        "settlements": len(settlements),
        "wins": sum(row.won for row in settlements),
        "realized_pnl": str(realized),
        "mean_brier_score": (
            sum(row.brier_score for row in settlements) / len(settlements) if settlements else None
        ),
    }


@router.get("/markets")
async def markets(request: Request) -> list[dict[str, object]]:
    return await market_rows(request)


@router.get("/markets/{market_id}")
async def market_detail(market_id: int, request: Request) -> dict[str, object]:
    async with request.app.state.sessions() as session:
        row = (
            await session.execute(select(Market, Event).join(Event).where(Market.id == market_id))
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="market not found")
        market, event = row
        rule = await session.scalar(
            select(NormalizedRule).where(NormalizedRule.market_id == market_id)
        )
        book = await session.scalar(
            select(OrderBookSnapshot)
            .where(OrderBookSnapshot.market_id == market_id)
            .order_by(OrderBookSnapshot.captured_at.desc())
        )
        probability = await session.scalar(
            select(ProbabilityEstimate)
            .where(ProbabilityEstimate.market_id == market_id)
            .order_by(ProbabilityEstimate.generated_at.desc())
        )
        runs = (
            await session.scalars(
                select(ForecastRun)
                .where(ForecastRun.market_id == market_id)
                .order_by(ForecastRun.retrieved_at.desc())
            )
        ).all()
        members = (
            (
                await session.scalars(
                    select(ForecastMember).where(
                        ForecastMember.forecast_run_id.in_([run.id for run in runs])
                    )
                )
            ).all()
            if runs
            else []
        )
        signals = (
            await session.scalars(
                select(Signal)
                .where(Signal.market_id == market_id)
                .order_by(Signal.generated_at.desc())
            )
        ).all()
        rejections = (
            await session.scalars(
                select(RejectedSignal)
                .where(RejectedSignal.market_id == market_id)
                .order_by(RejectedSignal.generated_at.desc())
            )
        ).all()
        values_by_run = {
            run.id: [member.daily_value for member in members if member.forecast_run_id == run.id]
            for run in runs
        }
    return {
        "id": market.id,
        "event": event.title,
        "question": market.question,
        "original_rules": event.original_rules,
        "resolution_source": market.resolution_source or event.resolution_source,
        "active": market.active,
        "closed": market.closed,
        "close_time": market.close_time,
        "liquidity": market.liquidity,
        "normalized_rule": (
            {
                "station_id": rule.station_id,
                "local_date": rule.local_date,
                "timezone": rule.timezone,
                "measurement": rule.measurement,
                "unit": rule.unit,
                "rounding_method": rule.rounding_method,
                "confidence_score": rule.confidence_score,
                "field_provenance": rule.field_provenance,
                "ambiguities": rule.ambiguities,
                "updated_at": rule.updated_at,
            }
            if rule
            else None
        ),
        "latest_order_book": (
            {
                "captured_at": book.captured_at,
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "spread": book.spread,
                "available_depth": book.available_depth,
            }
            if book
            else None
        ),
        "forecast_distributions": [
            {
                "provider": run.provider,
                "model": run.model,
                "retrieved_at": run.retrieved_at,
                "daily_values": values_by_run[run.id],
            }
            for run in runs
        ],
        "latest_probability": (
            {
                "generated_at": probability.generated_at,
                "outcomes": probability.outcome_probabilities,
                "valid_members": probability.valid_members,
                "excluded_members": probability.excluded_members,
                "ensemble_spread": probability.ensemble_spread,
                "uncertainty_score": probability.uncertainty_score,
            }
            if probability
            else None
        ),
        "accepted_signals": [
            {
                "generated_at": signal.generated_at,
                "model_probability": signal.model_probability,
                "executable_ask": signal.executable_ask,
                "usable_edge": signal.usable_edge,
            }
            for signal in signals
        ],
        "rejections": [
            {"generated_at": rejection.generated_at, "reasons": rejection.reasons}
            for rejection in rejections
        ],
    }


@router.get("/signals")
async def signals(request: Request) -> list[dict[str, object]]:
    return await signal_rows(request)


@router.get("/positions")
async def positions(request: Request) -> list[dict[str, object]]:
    return await position_rows(request)


@router.get("/performance")
async def performance(request: Request) -> dict[str, object]:
    return await performance_data(request)


@router.get("/errors")
async def errors(request: Request) -> list[dict[str, object]]:
    return await error_rows(request)


@router.get("/config")
async def config(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "product_name": PRODUCT_NAME,
        "compatibility_version": COMPATIBILITY_VERSION,
        "mode": "PAPER_ONLY",
        "app_env": settings.app_env,
        "real_trading_enabled": settings.real_trading_enabled,
        "polymarket_websocket_enabled": settings.polymarket_websocket_enabled,
        "telegram_configured": bool(
            settings.telegram_bot_token and settings.telegram_admin_user_id
        ),
        "forecast_models": settings.forecast_models,
        "min_rule_confidence": settings.min_rule_confidence,
        "min_ensemble_members": settings.min_ensemble_members,
        "min_usable_edge": settings.min_usable_edge,
        "max_spread": settings.max_spread,
        "min_liquidity_usd": settings.min_liquidity_usd,
    }
