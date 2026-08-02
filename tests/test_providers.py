import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.forecast import parse_open_meteo_response
from app.services.http import CircuitBreaker, ProviderUnavailable, ResilientHttpClient
from app.services.polymarket import parse_gamma_search, parse_order_book
from app.services.websocket import market_subscription

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_gamma_parser_decodes_binary_market_strings() -> None:
    events = parse_gamma_search(fixture("london_event.json"))

    assert len(events) == 1
    assert len(events[0].markets) == 3
    assert events[0].markets[1].outcomes == ("Yes", "No")
    assert events[0].markets[1].token_ids == ("yes-exact", "no-exact")
    assert events[0].markets[1].minimum_order_size == 5


def test_order_book_parser_uses_best_executable_prices_not_response_order() -> None:
    raw = fixture("london_books.json")
    assert isinstance(raw, list)

    book = parse_order_book(raw[0])

    assert book.best_bid == Decimal("0.44")
    assert book.best_ask == Decimal("0.47")
    assert book.spread == Decimal("0.03")
    assert book.midpoint == Decimal("0.455")
    assert book.available_depth == 15
    assert book.minimum_order_size == 5


def test_order_book_without_asks_has_no_executable_price() -> None:
    raw = fixture("london_books.json")
    assert isinstance(raw, list) and isinstance(raw[0], dict)
    raw[0]["asks"] = []

    book = parse_order_book(raw[0])

    assert book.best_ask is None
    assert book.spread is None
    assert book.midpoint is None


def test_open_meteo_parser_preserves_each_available_member() -> None:
    result = parse_open_meteo_response(
        fixture("london_ensemble.json"),
        model="gfs_seamless",
        retrieved_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
    )

    assert result.provider == "open_meteo"
    assert result.model == "gfs_seamless"
    assert len(result.members) == 3
    assert result.members[0].member_id == "member01"
    assert result.members[0].points[2].temperature == 22.4
    assert result.members[0].points[0].timestamp.utcoffset() is not None


def test_market_websocket_subscription_contains_only_public_asset_ids() -> None:
    assert market_subscription(("yes-low", "yes-high")) == {
        "assets_ids": ["yes-low", "yes-high"],
        "type": "market",
        "custom_feature_enabled": True,
    }


@pytest.mark.asyncio
async def test_resilient_client_retries_retryable_server_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resilient = ResilientHttpClient(client, max_retries=2, sleep=lambda _: asyncio.sleep(0))
        payload = await resilient.request_json("GET", "https://provider.test/data")

    assert payload == {"ok": True}
    assert attempts == 3


@pytest.mark.asyncio
async def test_circuit_opens_after_consecutive_failures() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "offline"})

    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resilient = ResilientHttpClient(client, max_retries=0, circuit_breaker=breaker)
        for _ in range(2):
            with pytest.raises(ProviderUnavailable):
                await resilient.request_json("GET", "https://provider.test/data")
        with pytest.raises(ProviderUnavailable, match="circuit"):
            await resilient.request_json("GET", "https://provider.test/data")

    assert attempts == 2
