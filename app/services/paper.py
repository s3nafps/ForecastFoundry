from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationSetting,
    PaperPosition,
    PaperSettlement,
    Signal,
)
from app.schemas import EntryQuote


class PaperTradingError(ValueError):
    pass


def estimate_entry(
    *, ask: Decimal, shares: Decimal, fee_rate: Decimal, slippage: Decimal
) -> EntryQuote:
    if ask < 0 or ask > 1 or shares <= 0 or fee_rate < 0 or slippage < 0:
        raise PaperTradingError("paper quote inputs are invalid")
    entry_price = min(Decimal("1"), ask + slippage)
    cost = entry_price * shares
    fees = cost * fee_rate
    return EntryQuote(
        entry_price=entry_price,
        shares=shares,
        cost=cost,
        fees=fees,
        total=cost + fees,
    )


async def get_paper_balance(session: AsyncSession, starting_balance: Decimal) -> Decimal:
    setting = await session.get(ApplicationSetting, "paper_balance")
    return Decimal(str(setting.value)) if setting else starting_balance


async def _set_paper_balance(
    session: AsyncSession, balance: Decimal, starting_balance: Decimal
) -> None:
    setting = await session.get(ApplicationSetting, "paper_balance")
    if setting is None:
        setting = ApplicationSetting(key="paper_balance", value=str(starting_balance))
        session.add(setting)
    setting.value = str(balance)


async def open_paper_position(
    session: AsyncSession,
    *,
    signal: Signal,
    shares: Decimal,
    minimum_order_size: Decimal,
    starting_balance: Decimal,
    fee_rate: Decimal,
    slippage: Decimal,
) -> PaperPosition:
    if shares < minimum_order_size:
        raise PaperTradingError("paper position is below the market minimum order size")
    existing = await session.scalar(
        select(PaperPosition).where(
            PaperPosition.market_id == signal.market_id,
            PaperPosition.outcome_id == signal.outcome_id,
            PaperPosition.status == "open",
        )
    )
    if existing:
        raise PaperTradingError("an overlapping paper position is already open")

    quote = estimate_entry(
        ask=signal.executable_ask,
        shares=shares,
        fee_rate=fee_rate,
        slippage=slippage,
    )
    balance = await get_paper_balance(session, starting_balance)
    if quote.total > balance:
        raise PaperTradingError("paper entry exceeds available balance")

    # ponytail: one scheduled SQLite writer; add a partial unique index before
    # allowing concurrent worker processes.
    position = PaperPosition(
        signal_id=signal.id,
        market_id=signal.market_id,
        outcome_id=signal.outcome_id,
        entered_at=datetime.now(UTC),
        entry_price=quote.entry_price,
        amount=quote.total,
        shares=quote.shares,
        fees=quote.fees,
        status="open",
        signal_data=signal.signal_data,
    )
    session.add(position)
    await _set_paper_balance(session, balance - quote.total, starting_balance)
    await session.flush()
    return position


async def settle_paper_position(
    session: AsyncSession, position_id: int, *, won: bool
) -> PaperSettlement:
    position = await session.get(PaperPosition, position_id)
    if position is None or position.status != "open":
        raise PaperTradingError("paper position is not open")
    signal = await session.get(Signal, position.signal_id)
    if signal is None:
        raise PaperTradingError("paper position signal is missing")
    balance = await get_paper_balance(session, Decimal("5"))
    payout = position.shares if won else Decimal("0")
    realized_pnl = payout - position.amount
    observed = Decimal("1") if won else Decimal("0")
    settlement = PaperSettlement(
        position_id=position.id,
        settled_at=datetime.now(UTC),
        won=won,
        payout=payout,
        realized_pnl=realized_pnl,
        brier_score=float((signal.model_probability - observed) ** 2),
        resolution_data={"manual": True},
    )
    position.status = "settled"
    session.add(settlement)
    await _set_paper_balance(session, balance + payout, Decimal("5"))
    await session.flush()
    return settlement
