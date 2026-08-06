from datetime import UTC, datetime, timedelta

import pytest

from app.providers.registry import ProviderRegistry, ProviderSecretError
from app.providers.research import sanitize_research_text
from app.providers.weather import request_headers


def test_default_registry_contains_authoritative_and_public_sources() -> None:
    registry = ProviderRegistry.default()

    assert registry.get("open_meteo_ensemble").auth == "none"
    assert registry.get("coinbase").classification == "authoritative"
    assert registry.get("reddit").classification == "feature_only"


def test_keyed_provider_requires_a_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ProviderRegistry.default()
    monkeypatch.delenv("WEATHERAPI_API_KEY", raising=False)

    with pytest.raises(ProviderSecretError, match="WEATHERAPI_API_KEY"):
        registry.resolve_secret("weatherapi")


def test_provider_health_marks_stale_data() -> None:
    registry = ProviderRegistry.default()
    now = datetime(2026, 8, 3, 12, tzinfo=UTC)

    health = registry.health("coinbase", retrieved_at=now - timedelta(minutes=10), now=now)

    assert health.healthy is False
    assert health.reason == "stale"


def test_weather_headers_use_identifying_user_agent() -> None:
    assert "ForecastFoundry" in request_headers("nws")["User-Agent"]
    assert "ForecastFoundry" in request_headers("met_no")["User-Agent"]


def test_research_text_is_bounded_and_control_characters_removed() -> None:
    clean = sanitize_research_text("hello\x00 world\n" + "x" * 5000)

    assert "\x00" not in clean
    assert len(clean) <= 4000
