from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Market, Outcome, PaperPosition, ProviderError
from app.services.paper import PaperTradingError, settle_paper_position
from app.services.polymarket import PolymarketClient


async def settle_resolved_markets(
    *,
    sessions: async_sessionmaker[AsyncSession],
    polymarket: PolymarketClient,
    starting_balance: Decimal,
    now: datetime,
) -> int:
    """Settle open paper positions whose Polymarket markets have resolved."""
    async with sessions() as session:
        positions = (
            await session.scalars(select(PaperPosition).where(PaperPosition.status == "open"))
        ).all()
        settled = 0
        for position in positions:
            outcome = await session.get(Outcome, position.outcome_id)
            market = await session.get(Market, position.market_id)
            if outcome is None or market is None or not market.condition_id:
                continue
            try:
                closed, winning_token_id = await polymarket.get_resolution(market.condition_id)
            except Exception as exc:
                session.add(
                    ProviderError(
                        provider="polymarket",
                        operation="resolution",
                        error_type=type(exc).__name__,
                        message=str(exc),
                        details={},
                        retryable=True,
                        occurred_at=now,
                    )
                )
                continue
            # Void/aborted markets have no winner and are left open for a later pass.
            if not closed or winning_token_id is None:
                continue
            try:
                await settle_paper_position(
                    session,
                    position.id,
                    won=outcome.token_id == winning_token_id,
                    starting_balance=starting_balance,
                )
            except PaperTradingError:
                continue
            settled += 1
        # Persist provider failures even when no position can be settled. Without
        # this commit an all-error pass silently loses its diagnostic records.
        await session.commit()
        return settled
