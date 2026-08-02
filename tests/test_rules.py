from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas import GammaEvent, GammaMarket, Station, TemperatureUnit
from app.services.rules import (
    RuleNormalizationError,
    load_market_overrides,
    load_station_registry,
    normalize_temperature_event,
    parse_bucket,
)

RULES = """This market will resolve to the temperature range that contains the highest
temperature recorded at the London City Airport Station in degrees Celsius on 2 Aug '26.
The resolution source is https://www.wunderground.com/history/daily/gb/london/EGLC.
The resolution source measures temperatures to whole degrees Celsius.
"""


def market(identifier: str, label: str) -> GammaMarket:
    return GammaMarket(
        id=identifier,
        question=f"Will the highest temperature in London be {label} on August 2?",
        group_item_title=label,
        condition_id=f"condition-{identifier}",
        outcomes=("Yes", "No"),
        token_ids=(f"yes-{identifier}", f"no-{identifier}"),
        active=True,
        closed=False,
        end_date=datetime(2026, 8, 2, 12, tzinfo=UTC),
        liquidity=1000,
        minimum_order_size=5,
        description=RULES,
        resolution_source="https://www.wunderground.com/history/daily/gb/london/EGLC",
        raw_data={},
    )


def london_event(
    labels: tuple[str, ...] = ("22°C or below", "23°C", "24°C or higher"),
) -> GammaEvent:
    return GammaEvent(
        id="775541",
        title="Highest temperature in London on August 2?",
        description=RULES,
        resolution_source="https://www.wunderground.com/history/daily/gb/london/EGLC",
        active=True,
        closed=False,
        end_date=datetime(2026, 8, 2, 12, tzinfo=UTC),
        markets=tuple(market(str(index), label) for index, label in enumerate(labels)),
        raw_data={},
    )


STATIONS = {
    "EGLC": Station(
        station_id="EGLC",
        name="London City Airport",
        latitude=51.505,
        longitude=0.055,
        timezone="Europe/London",
        source="https://aviationweather.gov/api/data/stationinfo?ids=EGLC&format=json",
    )
}


def test_bucket_parser_supports_lower_exact_and_upper_ranges() -> None:
    lower = parse_bucket("22°C or below", TemperatureUnit.CELSIUS)
    exact = parse_bucket("23°C", TemperatureUnit.CELSIUS)
    upper = parse_bucket("24°C or higher", TemperatureUnit.CELSIUS)

    assert (lower.lower, lower.upper) == (None, 22)
    assert (exact.lower, exact.upper) == (23, 23)
    assert (upper.lower, upper.upper) == (24, None)


def test_normalizes_explicit_london_rule_and_marks_rounding_inference() -> None:
    normalized = normalize_temperature_event(london_event(), STATIONS)

    assert normalized.location_name == "London"
    assert normalized.station_id == "EGLC"
    assert normalized.latitude == 51.505
    assert normalized.longitude == 0.055
    assert normalized.local_date.isoformat() == "2026-08-02"
    assert normalized.timezone == "Europe/London"
    assert normalized.measurement == "daily_max_temperature"
    assert normalized.unit is TemperatureUnit.CELSIUS
    assert normalized.confidence_score == 95
    assert normalized.field_provenance["rounding_method"].startswith("inferred:")
    assert normalized.ambiguities == ("rounding_method_inferred",)
    assert [bucket.label for bucket in normalized.buckets] == [
        "22°C or below",
        "23°C",
        "24°C or higher",
    ]


def test_rejects_a_gap_in_temperature_buckets() -> None:
    event = london_event(("22°C or below", "24°C", "25°C or higher"))

    with pytest.raises(RuleNormalizationError, match="gap"):
        normalize_temperature_event(event, STATIONS)


def test_rejects_rule_without_a_known_station() -> None:
    event = london_event().model_copy(
        update={
            "description": RULES.replace("EGLC", "ZZZZ"),
            "resolution_source": "https://example.com/ZZZZ",
        }
    )

    with pytest.raises(RuleNormalizationError, match="station"):
        normalize_temperature_event(event, STATIONS)


def test_override_can_supply_an_explicit_rounding_method() -> None:
    event = london_event().model_copy(
        update={
            "description": RULES.replace(
                "The resolution source measures temperatures to whole degrees Celsius.\n", ""
            )
        }
    )

    normalized = normalize_temperature_event(event, STATIONS, {"rounding_method": "half_up"})

    assert normalized.confidence_score == 100
    assert normalized.ambiguities == ()
    assert normalized.field_provenance["rounding_method"] == "override:market_overrides.yaml"


def test_loads_audited_station_registry() -> None:
    stations = load_station_registry(Path("config/stations.yaml"))

    assert stations["EGLC"].latitude == 51.505
    assert stations["EGLC"].source.startswith("https://aviationweather.gov/")


def test_rejects_unknown_override_fields(tmp_path: Path) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text("event-1:\n  magic_coordinate: 1\n", encoding="utf-8")

    with pytest.raises(RuleNormalizationError, match="unknown override"):
        load_market_overrides(path)
