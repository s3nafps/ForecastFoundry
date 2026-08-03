from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.database import make_engine
from app.main import create_app
from app.models import Base


@pytest.mark.asyncio
async def test_production_operator_controls_require_authorization_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'operator-header.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    monkeypatch.setenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "operator-token-123456")
    app = create_app(
        Settings(app_env="production", database_url=database_url, scheduler_enabled=False)
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body_token = await client.post(
                "/api/v1/operator/pause",
                json={"token": "operator-token-123456", "reason": "maintenance"},
            )
            assert body_token.status_code == 401
            header = await client.post(
                "/api/v1/operator/pause",
                headers={"Authorization": "Bearer operator-token-123456"},
                json={"reason": "maintenance", "request_id": "header-1"},
            )
            assert header.status_code == 200
            assert header.json()["paused"] is True
