from app.domains.base import MarketInput
from app.domains.registry import DomainRegistry
from app.domains.weather import WeatherPlugin


def test_strict_weather_plugin_rejects_missing_detailed_event() -> None:
    result = WeatherPlugin(strict=True).normalize(
        MarketInput(market_id="w-1", title="Will London reach 25°C?", description="temperature")
    )
    assert result.accepted is False
    assert result.reasons[0].startswith("weather_contract_invalid:")


def test_application_registry_keeps_legacy_route_compatible() -> None:
    result = DomainRegistry().route(
        MarketInput(market_id="w-2", title="Will London reach 25°C?", description="temperature")
    )
    assert result.accepted is True
    assert result.contract is not None
    assert "weather_contract_unresolved" in result.contract.ambiguities
