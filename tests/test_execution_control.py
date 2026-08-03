from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base, KillSwitchEvent, OperatorCredential
from app.services.application import ApplicationServices
from app.services.execution_control import ExecutionControl
from app.services.operator_auth import verify_hash


@pytest.mark.asyncio
async def test_control_is_durable_idempotent_and_audited(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        control = ExecutionControl(session)
        first = await control.snapshot()
        assert first.paused is True
        changed = await control.set_paused(
            False, actor="test", reason="resume paper loop", request_id="req-1"
        )
        again = await control.set_paused(
            True, actor="ignored", reason="ignored", request_id="req-1"
        )
        await session.commit()
    async with sessions() as session:
        persisted = await ExecutionControl(session).snapshot()
        events = (await session.scalars(select(KillSwitchEvent))).all()
    assert changed.paused is False
    assert again == changed
    assert persisted == changed
    assert len(events) == 1
    assert events[0].request_id == "req-1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_operator_token_is_hashed_expiring_and_permission_scoped(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'operator.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        from app.services.operator_auth import OperatorAuth

        auth = OperatorAuth(session, bootstrap_token="operator-token-123456")
        await auth.ensure_bootstrap()
        credential = await auth.authenticate("operator-token-123456", "execution:pause")
        assert credential.token_hash != "operator-token-123456"
        assert verify_hash("operator-token-123456", credential.token_hash)
        with pytest.raises(PermissionError):
            await auth.authenticate("operator-token-123456", "wallet:transfer")
        rows = (await session.scalars(select(OperatorCredential))).all()
        assert len(rows) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_service_persists_contract_routes(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'services.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    services = ApplicationServices(
        make_session_factory(engine), Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )
    result = await services.scan_markets(
        [
            {
                "market_id": "m-1",
                "title": "BTC above $100000 USD on Coinbase at 2026-12-31T00:00 UTC",
                "description": "closing price, rounded to cents",
            }
        ]
    )
    assert result["markets"][0]["accepted"] is True
    assert (await services.get_market_evidence("m-1"))["status"] == "no_evidence"
    await engine.dispose()
