"""Long-running scheduler service entrypoint used by the compose profile."""

import asyncio
import os

from app.config import Settings
from app.main import create_app


async def run() -> None:
    settings = Settings(
        app_env="scheduler",
        scheduler_enabled=True,
        database_url=os.getenv(
            "FORECASTFOUNDRY_DATABASE_URL", "sqlite+aiosqlite:///./data/weatheredge.db"
        ),
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
