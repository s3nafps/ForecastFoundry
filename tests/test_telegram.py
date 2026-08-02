from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.schemas import PaperAlert
from app.services.http import ResilientHttpClient
from app.services.telegram import TelegramClient, format_signal_alert

ALERT = PaperAlert(
    question="Highest temperature in London on August 2?",
    outcome="23°C",
    model_probability=Decimal("0.68"),
    executable_ask=Decimal("0.46"),
    raw_edge=Decimal("0.22"),
    usable_edge=Decimal("0.14"),
    model_member_counts={"GFS": (18, 31), "ECMWF": (25, 31)},
    station_id="EGLC",
    observation_summary="not collected",
    forecast_horizon_hours=19,
    spread=Decimal("0.03"),
    rule_confidence=95,
    generated_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
)


def test_formats_a_complete_paper_only_alert() -> None:
    message = format_signal_alert(ALERT)

    assert "WEATHEREDGE PAPER SIGNAL" in message
    assert "23°C" in message
    assert "Model probability: 68.0%" in message
    assert "Executable best ask: 46.0%" in message
    assert "GFS: 18/31 members" in message
    assert "Mode: PAPER ONLY" in message


@pytest.mark.asyncio
async def test_telegram_client_posts_formatted_message() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request=request, json=request.read().decode())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
        client = TelegramClient(ResilientHttpClient(raw, max_retries=0), token="token", chat_id=123)
        sent = await client.send_signal(ALERT)

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.url.path == "/bottoken/sendMessage"
    assert sent is True
