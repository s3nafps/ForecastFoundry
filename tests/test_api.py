from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.main import create_app
from app.models import ApplicationSetting, Base, Event, Market


@pytest.mark.asyncio
async def test_read_only_api_and_escaped_dashboard(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        event = Event(
            polymarket_id="event-1",
            title="London <script>alert(1)</script>",
            original_rules="Rules <script>alert(1)</script>",
            active=True,
            closed=False,
            raw_data={},
        )
        session.add(event)
        await session.flush()
        session.add(
            Market(
                event_id=event.id,
                polymarket_id="market-1",
                condition_id="condition-1",
                question="Will London reach 25 C?",
                active=True,
                closed=False,
                raw_data={},
            )
        )
        await session.commit()
    await engine.dispose()

    app = create_app(
        Settings(
            app_env="test",
            database_url=database_url,
            telegram_bot_token="secret-token",
        )
    )
    async with app.router.lifespan_context(app):
        assert await app.state.run_settlement() == {
            "status": "not_configured",
            "settled": 0,
            "reason": "authoritative_settlement_fetcher_missing",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in (
                "/health",
                "/ready",
                "/api/v1/markets",
                "/api/v1/signals",
                "/api/v1/positions",
                "/api/v1/performance",
                "/api/v1/errors",
                "/api/v1/config",
                "/",
                "/markets",
                "/markets/1",
                "/signals",
                "/positions",
                "/performance",
                "/errors",
                "/config",
            ):
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)

            config = await client.get("/api/v1/config")
            assert "secret-token" not in config.text
            assert "api.db" not in config.text
            assert config.json()["mode"] == "PAPER_ONLY"

            async with app.state.sessions() as session:
                session.add(ApplicationSetting(key="paused", value=True))
                await session.commit()
            paused = await client.post("/internal/scan")
            assert paused.json() == {"status": "paused"}
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/v1/markets")).status_code == 200

            overview = await client.get("/")
            assert "<script>alert(1)</script>" not in overview.text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in overview.text

            performance = (await client.get("/api/v1/performance")).json()
            assert performance["starting_balance"] == "5.00"
            assert performance["positions"] == 0
