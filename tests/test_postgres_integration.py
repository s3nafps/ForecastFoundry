import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("FORECASTFOUNDRY_TEST_POSTGRES") != "1",
    reason="PostgreSQL integration requires FORECASTFOUNDRY_TEST_POSTGRES=1",
)


async def test_postgres_migration_and_roundtrip() -> None:
    from app.database import make_engine, make_session_factory
    from app.models import ApplicationSetting

    url = os.getenv(
        "FORECASTFOUNDRY_TEST_POSTGRES_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres",
    )
    engine = make_engine(url)
    try:
        sessions = make_session_factory(engine)
        async with sessions() as session:
            row = ApplicationSetting(key="pg_probe", value="ok")
            session.add(row)
            await session.commit()
            stored = await session.get(ApplicationSetting, "pg_probe")
            assert stored is not None and stored.value == "ok"
    finally:
        await engine.dispose()
