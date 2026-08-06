from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.database import make_engine
from app.main import create_app
from app.models import Base


@pytest.mark.asyncio
async def test_general_agent_api_is_redacted_and_operator_controls_are_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'agent-api.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    monkeypatch.setenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "operator-secret")
    app = create_app(Settings(app_env="test", database_url=database_url))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            domains = await client.get("/api/v1/domains")
            assert domains.json()["domains"] == ["weather", "crypto"]

            health = await client.get("/api/v1/providers/health")
            assert health.status_code == 200
            assert "operator-secret" not in health.text

            status = await client.get("/api/v1/execution/status")
            assert status.json()["live_execution"] is False
            assert status.json()["mode"] == "PAPER_ONLY"

            denied = await client.post(
                "/api/v1/operator/pause", json={"token": "wrong", "reason": "test"}
            )
            assert denied.status_code == 401
            paused = await client.post(
                "/api/v1/operator/pause",
                json={"token": "operator-secret", "reason": "operator test"},
            )
            assert paused.json()["paused"] is True
            assert (await client.get("/api/v1/execution/status")).json()["paused"] is True

            resumed = await client.post(
                "/api/v1/operator/resume",
                json={
                    "token": "operator-secret",
                    "reason": "operator test",
                    "request_id": "api-control-1",
                },
            )
            assert resumed.json()["paused"] is False
            conflict = await client.post(
                "/api/v1/operator/pause",
                json={
                    "token": "operator-secret",
                    "reason": "operator test",
                    "request_id": "api-control-1",
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["type"] == "idempotency_conflict"


async def test_observation_ingest_job_is_registered_on_app_state(
    tmp_path: Path,
) -> None:
    from app.main import create_app

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'observation-ingest.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    application = create_app(Settings(app_env="test", database_url=database_url))

    async with application.router.lifespan_context(application):
        assert hasattr(application.state, "run_observation_ingest")
