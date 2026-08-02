import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence

from websockets.asyncio.client import connect

from app.schemas import OrderBook
from app.services.polymarket import parse_order_book


def market_subscription(token_ids: Sequence[str]) -> dict[str, object]:
    return {
        "assets_ids": list(token_ids),
        "type": "market",
        "custom_feature_enabled": True,
    }


class MarketWebSocket:
    def __init__(
        self,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    ) -> None:
        self._url = url

    async def run(
        self,
        token_ids: Sequence[str],
        on_book: Callable[[OrderBook], Awaitable[None]],
    ) -> None:
        async with connect(self._url) as websocket:
            await websocket.send(json.dumps(market_subscription(token_ids)))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(10)
                    await websocket.send("PING")

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                async for message in websocket:
                    if not isinstance(message, str) or message == "PONG":
                        continue
                    payload = json.loads(message)
                    events = payload if isinstance(payload, list) else [payload]
                    for event in events:
                        if isinstance(event, dict) and event.get("event_type") == "book":
                            await on_book(parse_order_book(event))
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
