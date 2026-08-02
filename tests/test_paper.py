from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.database import make_engine, make_session_factory
from app.models import Base, Event, Market, Outcome, Signal
from app.services.paper import (
    PaperTradingError,
    estimate_entry,
    get_paper_balance,
    open_paper_position,
    settle_paper_position,
)


def test_entry_quote_includes_slippage_and_fees() -> None:
    quote = estimate_entry(
        ask=Decimal("0.46"),
        shares=Decimal("5"),
        fee_rate=Decimal("0.02"),
        slippage=Decimal("0.01"),
    )

    assert quote.entry_price == Decimal("0.47")
    assert quote.cost == Decimal("2.35")
    assert quote.fees == Decimal("0.047")
    assert quote.total == Decimal("2.397")


async def seeded_session(database_path: Path) -> tuple[AsyncEngine, AsyncSession, Signal]:
    engine = make_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    session = sessions()
    event = Event(
        polymarket_id="event",
        title="London temperature",
        original_rules="rules",
        active=True,
        closed=False,
        raw_data={},
    )
    session.add(event)
    await session.flush()
    market = Market(
        event_id=event.id,
        polymarket_id="market",
        condition_id="condition",
        question="23°C?",
        active=True,
        closed=False,
        raw_data={},
    )
    session.add(market)
    await session.flush()
    outcome = Outcome(market_id=market.id, label="Yes", token_id="yes-token")
    session.add(outcome)
    await session.flush()
    signal = Signal(
        market_id=market.id,
        outcome_id=outcome.id,
        generated_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
        model_probability=Decimal("0.68"),
        executable_ask=Decimal("0.46"),
        raw_edge=Decimal("0.22"),
        usable_edge=Decimal("0.14"),
        buffers={},
        fingerprint="fingerprint",
        signal_data={},
    )
    session.add(signal)
    await session.commit()
    return engine, session, signal


@pytest.mark.asyncio
async def test_paper_entry_obeys_balance_minimum_and_duplicate_limits(tmp_path: Path) -> None:
    engine, session, signal = await seeded_session(tmp_path / "paper.db")

    with pytest.raises(PaperTradingError, match="minimum"):
        await open_paper_position(
            session,
            signal=signal,
            shares=Decimal("4"),
            minimum_order_size=Decimal("5"),
            starting_balance=Decimal("5"),
            fee_rate=Decimal("0.02"),
            slippage=Decimal("0.01"),
        )

    position = await open_paper_position(
        session,
        signal=signal,
        shares=Decimal("5"),
        minimum_order_size=Decimal("5"),
        starting_balance=Decimal("5"),
        fee_rate=Decimal("0.02"),
        slippage=Decimal("0.01"),
    )
    await session.commit()

    assert position.amount == Decimal("2.397")
    assert await get_paper_balance(session, Decimal("5")) == Decimal("2.603")
    with pytest.raises(PaperTradingError, match="overlapping"):
        await open_paper_position(
            session,
            signal=signal,
            shares=Decimal("5"),
            minimum_order_size=Decimal("5"),
            starting_balance=Decimal("5"),
            fee_rate=Decimal("0.02"),
            slippage=Decimal("0.01"),
        )
    await session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_paper_entry_rejects_total_cost_above_balance(tmp_path: Path) -> None:
    engine, session, signal = await seeded_session(tmp_path / "insufficient.db")
    signal.executable_ask = Decimal("0.99")

    with pytest.raises(PaperTradingError, match="balance"):
        await open_paper_position(
            session,
            signal=signal,
            shares=Decimal("5"),
            minimum_order_size=Decimal("5"),
            starting_balance=Decimal("5"),
            fee_rate=Decimal("0.02"),
            slippage=Decimal("0.01"),
        )
    await session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_winning_settlement_returns_share_payout_to_balance(tmp_path: Path) -> None:
    engine, session, signal = await seeded_session(tmp_path / "settlement.db")
    position = await open_paper_position(
        session,
        signal=signal,
        shares=Decimal("5"),
        minimum_order_size=Decimal("5"),
        starting_balance=Decimal("5"),
        fee_rate=Decimal("0.02"),
        slippage=Decimal("0.01"),
    )
    await session.commit()

    settlement = await settle_paper_position(session, position.id, won=True)
    await session.commit()

    assert settlement.payout == Decimal("5")
    assert settlement.realized_pnl == Decimal("2.603")
    assert await get_paper_balance(session, Decimal("5")) == Decimal("7.603")
    await session.close()
    await engine.dispose()
