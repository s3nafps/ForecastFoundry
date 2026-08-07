from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base
from app.services.execution_control import ExecutionControl
from app.services.executor import Executor, ExecutorError, OrderIntent
from app.services.risk import RiskDecision


class FakeExchange:
    def __init__(self) -> None:
        self.submissions = 0

    async def submit(self, *, client_order_id: str, price: Decimal, size: Decimal) -> str:
        self.submissions += 1
        return "provider-1"

    async def status(self, provider_order_id: str) -> dict[str, object]:
        return {"status": "filled"}


@pytest.mark.asyncio
async def test_paper_executor_is_idempotent_and_uses_no_exchange_call(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'executor.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    fake = FakeExchange()
    risk = RiskDecision(True, Decimal("2"), Decimal("1"))
    async with sessions() as session:
        await ExecutionControl(session).set_paused(False, actor="test", reason="paper test")
        executor = Executor(session, Settings(app_env="test"), adapter=fake)
        order = await executor.submit(
            OrderIntent("client-1", "paper", Decimal("0.5"), Decimal("2")), risk
        )
        duplicate = await executor.submit(
            OrderIntent("client-1", "paper", Decimal("0.5"), Decimal("2")), risk
        )
        assert order.id == duplicate.id
        assert order.mode == "paper"
        assert fake.submissions == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_executor_refuses_entries_when_durable_control_is_paused(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'executor-paused.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        executor = Executor(session, Settings(app_env="test"))
        with pytest.raises(ExecutorError, match="paused"):
            await executor.submit(
                OrderIntent("client-2", "paper", Decimal("0.5"), Decimal("2")),
                RiskDecision(True, Decimal("2"), Decimal("1")),
            )
    await engine.dispose()
