import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.observations import (
    ObservedHour,
    apply_observations_to_points,
    parse_aviation_weather_observations,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_aviation_weather_observations_returns_typed_hours() -> None:
    payload = json.loads((FIXTURES / "aviationweather_eglc.json").read_text(encoding="utf-8"))
    rows = parse_aviation_weather_observations(payload, station_id="EGLC")

    assert [row.station_id for row in rows] == ["EGLC", "EGLC", "EGLC"]
    assert rows[0].observed_at == datetime.fromtimestamp(1785600000, UTC)
    assert rows[0].temperature_celsius == 19.8
    assert rows[2].raw_ob.startswith("EGLC 010200Z")


def test_parse_drops_rows_with_fatal_quality_flags() -> None:
    payload = json.loads((FIXTURES / "aviationweather_eglc.json").read_text(encoding="utf-8"))
    rows = parse_aviation_weather_observations(payload, station_id="EGLC")

    assert all("missing_temperature" not in row.quality_flags for row in rows)
    assert len(rows) == 3


def test_parse_rejects_wrong_station_or_malformed_rows() -> None:
    from app.services.observations import ObservationParseError

    payload = [
        {"icaoId": "EGLL", "obsTime": 1785600000, "temp": 20.0},
        {"icaoId": "EGLC", "obsTime": "not-a-number", "temp": 20.0},
    ]
    with pytest.raises(ObservationParseError):
        parse_aviation_weather_observations(payload, station_id="EGLC")


def test_apply_observations_replaces_past_points_and_keeps_future() -> None:
    from app.schemas import ForecastPoint

    points = (
        ForecastPoint(timestamp=datetime(2026, 8, 1, 22, 0, tzinfo=UTC), temperature=10.0),
        ForecastPoint(timestamp=datetime(2026, 8, 1, 23, 0, tzinfo=UTC), temperature=11.0),
        ForecastPoint(timestamp=datetime(2026, 8, 2, 0, 0, tzinfo=UTC), temperature=12.0),
    )
    observations = (
        ObservedHour(
            station_id="EGLC",
            observed_at=datetime(2026, 8, 1, 22, 30, tzinfo=UTC),
            temperature_celsius=20.0,
            raw_ob="EGLC fixture",
            quality_flags=(),
        ),
    )
    now = datetime(2026, 8, 1, 23, 30, tzinfo=UTC)

    reconciled = apply_observations_to_points(points, observations, now=now)

    assert reconciled[0].temperature == 20.0
    assert reconciled[1].temperature == 20.0
    assert reconciled[2].temperature == 12.0


def test_apply_observations_uses_latest_observation_without_future_leak() -> None:
    from app.schemas import ForecastPoint

    points = (
        ForecastPoint(timestamp=datetime(2026, 8, 1, 22, 0, tzinfo=UTC), temperature=10.0),
        ForecastPoint(timestamp=datetime(2026, 8, 1, 23, 0, tzinfo=UTC), temperature=11.0),
    )
    observations = (
        ObservedHour("EGLC", datetime(2026, 8, 1, 22, 30, tzinfo=UTC), 18.0, "a", ()),
        ObservedHour("EGLC", datetime(2026, 8, 1, 23, 30, tzinfo=UTC), 19.0, "b", ()),
    )
    now = datetime(2026, 8, 1, 23, 45, tzinfo=UTC)

    reconciled = apply_observations_to_points(points, observations, now=now)

    assert reconciled[0].temperature == 18.0
    assert reconciled[1].temperature == 19.0
