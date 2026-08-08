from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.database import make_engine, make_session_factory
from app.models import (
    ApplicationSetting,
    Base,
    Event,
    Market,
    Outcome,
    PaperPosition,
    PaperSettlement,
    ProviderError,
    Signal,
)
from app.services.settlement import settle_resolved_markets
from app.worker import maybe_alert_provider_errors

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class StubPolymarket:
    def __init__(self, resolution: tuple[bool, str | None]) -> None:
        self.resolution = resolution

    async def get_resolution(self, condition_id: str) -> tuple[bool, str | None]:
        return self.resolution


class FailingPolymarket:
    async def get_resolution(self, condition_id: str) -> tuple[bool, str | None]:
        raise RuntimeError(f"resolution unavailable for {condition_id}")


class RecordingTelegram:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_text(self, text: str) -> bool:
        self.texts.append(text)
        return True


async def seeded_position(
    database_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        event = Event(
            polymarket_id="event",
            title="Market",
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
            condition_id="condition-x",
            question="?",
            active=False,
            closed=True,
            raw_data={},
        )
        session.add(market)
        await session.flush()
        yes = Outcome(market_id=market.id, label="Yes", token_id="yes-token")
        no = Outcome(market_id=market.id, label="No", token_id="no-token")
        session.add_all((yes, no))
        await session.flush()
        signal = Signal(
            market_id=market.id,
            outcome_id=yes.id,
            generated_at=NOW,
            model_probability=Decimal("0.68"),
            executable_ask=Decimal("0.46"),
            raw_edge=Decimal("0.22"),
            usable_edge=Decimal("0.14"),
            buffers={},
            fingerprint="fp",
            signal_data={},
        )
        session.add(signal)
        await session.flush()
        session.add(
            PaperPosition(
                signal_id=signal.id,
                market_id=market.id,
                outcome_id=yes.id,
                entered_at=NOW,
                entry_price=Decimal("0.47"),
                amount=Decimal("2.35"),
                shares=Decimal("5"),
                fees=Decimal("0"),
                status="open",
                signal_data={},
            )
        )
        # Balance after the entry above deducted 2.35 from the starting 5.00.
        session.add(ApplicationSetting(key="paper_balance", value="2.65"))
        await session.commit()
    return engine, sessions


@pytest.mark.asyncio
async def test_winning_position_is_settled_and_balance_credited(tmp_path: Path) -> None:
    engine, sessions = await seeded_position(tmp_path / "win.db")
    try:
        settled = await settle_resolved_markets(
            sessions=sessions,
            polymarket=StubPolymarket((True, "yes-token")),
            starting_balance=Decimal("5.00"),
            now=NOW,
        )
        async with sessions() as session:
            position = await session.scalar(select(PaperPosition))
            balance = await session.get(ApplicationSetting, "paper_balance")
            settlement = await session.scalar(select(PaperSettlement))
        assert settled == 1
        assert position is not None and position.status == "settled"
        assert settlement is not None and settlement.won is True
        assert settlement.payout == Decimal("5")
        assert settlement.brier_score == pytest.approx((0.68 - 1) ** 2)
        assert balance is not None and Decimal(str(balance.value)) == Decimal("7.65")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_losing_position_settles_with_zero_payout(tmp_path: Path) -> None:
    engine, sessions = await seeded_position(tmp_path / "lose.db")
    try:
        settled = await settle_resolved_markets(
            sessions=sessions,
            polymarket=StubPolymarket((True, "no-token")),
            starting_balance=Decimal("5.00"),
            now=NOW,
        )
        async with sessions() as session:
            settlement = await session.scalar(select(PaperSettlement))
        assert settled == 1
        assert settlement is not None and settlement.won is False
        assert settlement.payout == Decimal("0")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_void_market_keeps_position_open(tmp_path: Path) -> None:
    engine, sessions = await seeded_position(tmp_path / "void.db")
    try:
        settled = await settle_resolved_markets(
            sessions=sessions,
            polymarket=StubPolymarket((True, None)),
            starting_balance=Decimal("5.00"),
            now=NOW,
        )
        async with sessions() as session:
            position = await session.scalar(select(PaperPosition))
        assert settled == 0
        assert position is not None and position.status == "open"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolution_error_is_persisted_when_no_position_settles(tmp_path: Path) -> None:
    engine, sessions = await seeded_position(tmp_path / "resolution-error.db")
    try:
        settled = await settle_resolved_markets(
            sessions=sessions,
            polymarket=FailingPolymarket(),
            starting_balance=Decimal("5.00"),
            now=NOW,
        )
        async with sessions() as session:
            position = await session.scalar(select(PaperPosition))
            errors = (await session.scalars(select(ProviderError))).all()

        assert settled == 0
        assert position is not None and position.status == "open"
        assert len(errors) == 1
        assert errors[0].operation == "resolution"
        assert errors[0].error_type == "RuntimeError"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_error_alert_sends_once_per_hour_above_threshold(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 'errors.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    telegram = RecordingTelegram()
    try:
        async with sessions() as session:
            for _ in range(3):
                session.add(
                    ProviderError(
                        provider="polymarket",
                        operation="discover",
                        error_type="ProviderUnavailable",
                        message="down",
                        details={},
                        retryable=True,
                        occurred_at=NOW - timedelta(minutes=2),
                    )
                )
            await session.commit()

        await maybe_alert_provider_errors(
            sessions=sessions, telegram=telegram, threshold=3, now=NOW
        )
        await maybe_alert_provider_errors(
            sessions=sessions, telegram=telegram, threshold=3, now=NOW + timedelta(minutes=5)
        )
        async with sessions() as session:
            setting = await session.get(ApplicationSetting, "last_error_alert_at")

        assert len(telegram.texts) == 1
        assert "3 provider errors" in telegram.texts[0]
        assert setting is not None and setting.value
    finally:
        await engine.dispose()
