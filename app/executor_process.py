"""Dedicated executor process bootstrap; paper mode remains the default."""

import asyncio

from app.config import Settings
from app.services.execution_policy import assert_startup_safe


async def run() -> None:
    settings = Settings(app_env="executor")
    # Startup validation is intentionally fail-closed. The process does not
    # create a client or submit anything until an explicit adapter is wired.
    assert_startup_safe(settings)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
