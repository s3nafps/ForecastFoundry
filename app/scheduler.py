"""Long-running scheduler service entrypoint used by the compose profile."""

import asyncio

from app.config import Settings
from app.main import create_app


async def run() -> None:
    settings = Settings(app_env="scheduler", scheduler_enabled=True)
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
