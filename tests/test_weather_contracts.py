import json
from datetime import UTC, date, datetime
from pathlib import Path

from app.domains.base import MarketInput
from app.domains.registry import DomainRegistry
from app.domains.weather import WeatherPlugin
from app.services.polymarket import parse_gamma_search


def test_weather_plugin_rejects_missing_detailed_event_by_default() -> None:
    result = WeatherPlugin().normalize(
        MarketInput(market_id="w-1", title="Will London reach 25°C?", description="temperature")
    )
    assert result.accepted is False
    assert result.reasons[0].startswith("weather_contract_invalid:")


def test_domain_registry_rejects_unresolved_weather_by_default() -> None:
    result = DomainRegistry().route(
        MarketInput(market_id="w-2", title="Will London reach 25°C?", description="temperature")
    )
    assert result.accepted is False
    assert result.domain == "weather"
    assert result.contract is None


def test_legacy_unresolved_weather_requires_explicit_compatibility_constructor() -> None:
    result = DomainRegistry(allow_legacy_unresolved_weather=True).route(
        MarketInput(market_id="w-2", title="Will London reach 25°C?", description="temperature")
    )
    assert result.accepted is True
    assert result.contract is not None
    assert "weather_contract_unresolved" in result.contract.ambiguities


def test_precipitation_is_unsupported_until_a_complete_normalizer_exists() -> None:
    result = DomainRegistry().route(
        MarketInput(
            market_id="rain-1",
            title="Will London rainfall exceed 10 mm?",
            description="daily precipitation",
        )
    )

    assert result.accepted is False
    assert result.domain is None
    assert result.reasons == ("unsupported_domain",)


def test_precipitation_is_unsupported_even_with_temperature_tokens() -> None:
    result = DomainRegistry().route(
        MarketInput(
            market_id="rain-temperature",
            title="Will rainfall occur while the temperature exceeds 25°C?",
            description="daily precipitation and temperature",
        )
    )

    assert result.accepted is False
    assert result.reasons == ("unsupported_domain",)


def test_strict_weather_contract_preserves_detailed_normalized_rules() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "london_event.json").read_text(encoding="utf-8")
    )
    event = parse_gamma_search(payload)[0]
    raw_event = event.model_dump(mode="json")
    raw_event["description"] = event.description.replace(
        "measures temperatures to whole degrees Celsius", "requires temperatures to round down"
    )

    result = DomainRegistry().route(
        MarketInput(
            market_id="3237363",
            title=event.title,
            description=str(raw_event["description"]),
            raw_data={"event": raw_event},
        )
    )

    assert result.accepted is True
    assert result.contract is not None
    contract = result.contract
    assert contract.event_id == "775541"
    assert contract.location_name == "London"
    assert contract.station_id == "EGLC"
    assert contract.latitude == 51.505
    assert contract.longitude == 0.055
    assert contract.local_date == date(2026, 8, 2)
    assert contract.timezone == "Europe/London"
    assert contract.measurement == "daily_max_temperature"
    assert contract.expiry == datetime(2026, 8, 2, 12, tzinfo=UTC)
    assert contract.resolution_source.endswith("/EGLC")
    assert contract.unit_or_quote == "celsius"
    assert contract.reporting_period == "local_calendar_day"
    assert contract.rounding_method == "floor"
    assert contract.buckets[0]["upper_inclusive"] is True
    assert contract.provenance["local_date"] == "explicit:rules"
    assert contract.original_rules == raw_event["description"]


def test_weather_market_id_selects_its_own_expiry() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "london_event.json").read_text(encoding="utf-8")
    )
    event = parse_gamma_search(payload)[0]
    raw_event = event.model_dump(mode="json")
    raw_event["description"] = event.description.replace(
        "measures temperatures to whole degrees Celsius", "requires temperatures to round down"
    )
    raw_event["end_date"] = "2026-08-04T12:00:00Z"
    raw_event["markets"][1]["end_date"] = "2026-08-03T18:30:00Z"

    market_route = DomainRegistry().route(
        MarketInput(
            market_id="3237363",
            title=event.title,
            description=str(raw_event["description"]),
            raw_data={"event": raw_event},
        )
    )
    event_route = DomainRegistry().route(
        MarketInput(
            market_id="775541",
            title=event.title,
            description=str(raw_event["description"]),
            raw_data={"event": raw_event},
        )
    )

    assert market_route.accepted is True
    assert market_route.contract is not None
    assert market_route.contract.expiry == datetime(2026, 8, 3, 18, 30, tzinfo=UTC)
    assert event_route.accepted is True
    assert event_route.contract is not None
    assert event_route.contract.expiry == datetime(2026, 8, 4, 12, tzinfo=UTC)


def test_weather_rejects_market_id_outside_supplied_event() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "london_event.json").read_text(encoding="utf-8")
    )
    event = parse_gamma_search(payload)[0]
    raw_event = event.model_dump(mode="json")
    raw_event["description"] = event.description.replace(
        "measures temperatures to whole degrees Celsius", "requires temperatures to round down"
    )

    route = DomainRegistry().route(
        MarketInput(
            market_id="different-market",
            title=event.title,
            description=str(raw_event["description"]),
            raw_data={"event": raw_event},
        )
    )

    assert route.accepted is False
    assert route.reasons == ("weather_contract_invalid:market_id_not_in_supplied_event",)


def test_weather_rejects_missing_selected_expiry() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "london_event.json").read_text(encoding="utf-8")
    )
    event = parse_gamma_search(payload)[0]
    raw_event = event.model_dump(mode="json")
    raw_event["description"] = event.description.replace(
        "measures temperatures to whole degrees Celsius", "requires temperatures to round down"
    )
    raw_event["markets"][1]["end_date"] = None

    route = DomainRegistry().route(
        MarketInput(
            market_id="3237363",
            title=event.title,
            description=str(raw_event["description"]),
            raw_data={"event": raw_event},
        )
    )

    assert route.accepted is False
    assert route.reasons == ("weather_contract_invalid:expiry_missing",)
