import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base, KillSwitchEvent, OperatorCredential
from app.services.application import ApplicationServices
from app.services.execution_control import ExecutionControl, IdempotencyConflict, RevisionConflict
from app.services.operator_auth import verify_hash


@pytest.mark.asyncio
async def test_identical_retry_returns_original_result_without_another_audit(
    tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        control = ExecutionControl(session)
        first = await control.snapshot()
        assert first.paused is True
        changed = await control.set_paused(
            False,
            actor="test",
            reason=" resume paper loop ",
            request_id="req-1",
            expected_revision=0,
        )
        again = await control.set_paused(
            False,
            actor=" test ",
            reason="resume paper loop",
            request_id="req-1",
            expected_revision=0,
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
@pytest.mark.parametrize(
    "changed",
    [
        {
            "paused": True,
            "actor": "test",
            "reason": "resume paper loop",
            "expected_revision": 0,
        },
        {
            "paused": False,
            "actor": "other",
            "reason": "resume paper loop",
            "expected_revision": 0,
        },
        {
            "paused": False,
            "actor": "test",
            "reason": "different reason",
            "expected_revision": 0,
        },
        {
            "paused": False,
            "actor": "test",
            "reason": "resume paper loop",
            "expected_revision": 1,
        },
    ],
    ids=["opposite-target", "changed-actor", "changed-reason", "changed-expected-revision"],
)
async def test_request_id_rejects_changed_bound_fields(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'bound-fields.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        control = ExecutionControl(session)
        original = await control.set_paused(
            False,
            actor="test",
            reason="resume paper loop",
            request_id="bound-1",
            expected_revision=0,
        )
        with pytest.raises(IdempotencyConflict):
            await control.set_paused(
                bool(changed["paused"]),
                actor=str(changed["actor"]),
                reason=str(changed["reason"]),
                request_id="bound-1",
                expected_revision=int(changed["expected_revision"]),
            )
        assert (await control.snapshot()) == original
        assert len((await session.scalars(select(KillSwitchEvent))).all()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_expected_revision_leaves_state_unchanged(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale-revision.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        await ExecutionControl(session).snapshot()
        await session.commit()
    async with sessions() as session:
        control = ExecutionControl(session)
        original = await control.set_paused(
            False, actor="test", reason="resume", request_id="fresh", expected_revision=0
        )
        with pytest.raises(RevisionConflict, match="revision conflict"):
            await control.set_paused(
                True, actor="test", reason="pause", request_id="stale", expected_revision=0
            )
        assert (await control.snapshot()) == original
        assert len((await session.scalars(select(KillSwitchEvent))).all()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_one_result(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-retry.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        await ExecutionControl(session).snapshot()
        await session.commit()

    async def transition() -> object:
        async with sessions() as session:
            result = await ExecutionControl(session).set_paused(
                False,
                actor="test",
                reason="resume",
                request_id="concurrent-retry",
                expected_revision=0,
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(transition(), transition())
    assert first == second
    async with sessions() as session:
        assert len((await session.scalars(select(KillSwitchEvent))).all()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_database_concurrent_identical_requests_share_one_result(
    tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh-concurrent-retry.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    first_checks = asyncio.Barrier(2)

    class SynchronizedExecutionControl(ExecutionControl):
        initial_check = True

        async def _retry(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = await super()._retry(*args, **kwargs)
            if self.initial_check:
                self.initial_check = False
                await first_checks.wait()
            return result

    async def transition() -> object:
        async with sessions() as session:
            result = await SynchronizedExecutionControl(session).set_paused(
                False,
                actor="test",
                reason="resume",
                request_id="fresh-concurrent-retry",
                expected_revision=0,
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(transition(), transition())
    assert first == second
    async with sessions() as session:
        assert len((await session.scalars(select(KillSwitchEvent))).all()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_requests_allow_one_transition_per_revision(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        await ExecutionControl(session).snapshot()
        await session.commit()

    async def transition(request_id: str, paused: bool) -> object:
        async with sessions() as session:
            try:
                result = await ExecutionControl(session).set_paused(
                    paused,
                    actor="test",
                    reason=request_id,
                    request_id=request_id,
                    expected_revision=0,
                )
                await session.commit()
                return result
            except RuntimeError as exc:
                await session.rollback()
                return exc

    results = await asyncio.gather(
        transition("concurrent-pause", True), transition("concurrent-resume", False)
    )
    assert sum(type(result).__name__ == "RevisionConflict" for result in results) == 1
    async with sessions() as session:
        assert (await ExecutionControl(session).snapshot()).revision == 1
        assert len((await session.scalars(select(KillSwitchEvent))).all()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_returns_original_result_across_fresh_sessions(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'persistent-idempotency.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        first = await ExecutionControl(session).set_paused(
            False, actor="test", reason="resume", request_id="persisted-1", expected_revision=0
        )
        await session.commit()
    async with sessions() as session:
        await ExecutionControl(session).set_paused(
            True, actor="test", reason="pause", request_id="persisted-2", expected_revision=1
        )
        await session.commit()
    await engine.dispose()
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        retry = await ExecutionControl(session).set_paused(
            False, actor="test", reason="resume", request_id="persisted-1", expected_revision=0
        )
        events = (await session.scalars(select(KillSwitchEvent))).all()
    assert retry == first
    assert retry.revision == 1
    assert len(events) == 2
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
