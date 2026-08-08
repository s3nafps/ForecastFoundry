from datetime import UTC, date, datetime

import pytest

from app.schemas import (
    Bucket,
    ForecastPoint,
    MemberDailyValue,
    RoundingMethod,
    TemperatureUnit,
)
from app.services.probability import (
    calculate_probabilities,
    celsius_to_fahrenheit,
    daily_maximum,
    fahrenheit_to_celsius,
    round_temperature,
)


def test_temperature_conversions() -> None:
    assert fahrenheit_to_celsius(32) == pytest.approx(0)
    assert fahrenheit_to_celsius(212) == pytest.approx(100)
    assert celsius_to_fahrenheit(0) == pytest.approx(32)
    assert celsius_to_fahrenheit(100) == pytest.approx(212)


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        (RoundingMethod.HALF_UP, 24),
        (RoundingMethod.FLOOR, 23),
        (RoundingMethod.CEILING, 24),
    ],
)
def test_rounding_rules(method: RoundingMethod, expected: int) -> None:
    assert round_temperature(23.5, method) == expected


def test_daily_maximum_uses_the_market_local_calendar_day() -> None:
    points = (
        ForecastPoint(timestamp=datetime(2026, 8, 1, 22, 30, tzinfo=UTC), temperature=21),
        ForecastPoint(timestamp=datetime(2026, 8, 2, 12, 0, tzinfo=UTC), temperature=27),
        ForecastPoint(timestamp=datetime(2026, 8, 2, 23, 30, tzinfo=UTC), temperature=30),
    )

    assert daily_maximum(points, date(2026, 8, 2), "Europe/London") == 27


def test_probability_weights_models_equally_not_members() -> None:
    buckets = (
        Bucket(label="22°C or below", upper=22),
        Bucket(label="23°C", lower=23, upper=23),
        Bucket(label="24°C or higher", lower=24),
    )
    members = (
        MemberDailyValue(model="ecmwf", member_id="1", value=22.2),
        MemberDailyValue(model="ecmwf", member_id="2", value=23.1),
        MemberDailyValue(model="gfs", member_id="1", value=24.2),
        MemberDailyValue(model="gfs", member_id="2", value=None, exclusion_reason="missing"),
    )

    result = calculate_probabilities(
        members,
        buckets,
        rounding_method=RoundingMethod.HALF_UP,
        unit=TemperatureUnit.CELSIUS,
        model_weights={},
    )

    assert result.valid_members == 3
    assert result.excluded_members == 1
    assert result.outcome_probabilities == pytest.approx(
        {"22°C or below": 0.25, "23°C": 0.25, "24°C or higher": 0.5}
    )
    assert sum(result.outcome_probabilities.values()) == pytest.approx(1)
    assert result.uncertainty_score == pytest.approx(0.5)


def test_bias_is_applied_before_rounding_and_mapping() -> None:
    buckets = (
        Bucket(label="23°C or below", upper=23),
        Bucket(label="24°C or higher", lower=24),
    )
    member = MemberDailyValue(model="gfs", member_id="1", value=23.4, bias_correction=0.2)

    result = calculate_probabilities(
        (member,),
        buckets,
        rounding_method=RoundingMethod.HALF_UP,
        unit=TemperatureUnit.CELSIUS,
        model_weights={},
    )

    assert result.outcome_probabilities["24°C or higher"] == 1


def test_fahrenheit_buckets_convert_celsius_members_before_rounding() -> None:
    buckets = (
        Bucket(label="79°F or below", upper=79),
        Bucket(label="80°F or higher", lower=80),
    )
    members = (
        MemberDailyValue(model="gfs", member_id="1", value=26.0),  # 78.8°F rounds to 79
        MemberDailyValue(model="gfs", member_id="2", value=27.0),  # 80.6°F rounds to 81
    )

    result = calculate_probabilities(
        members,
        buckets,
        rounding_method=RoundingMethod.HALF_UP,
        unit=TemperatureUnit.FAHRENHEIT,
        model_weights={},
    )

    assert result.outcome_probabilities == pytest.approx(
        {"79°F or below": 0.5, "80°F or higher": 0.5}
    )
