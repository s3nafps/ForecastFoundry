from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from statistics import pstdev
from zoneinfo import ZoneInfo

from app.schemas import (
    Bucket,
    ForecastPoint,
    MemberDailyValue,
    ProbabilityResult,
    RoundingMethod,
    TemperatureUnit,
)


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32) * 5 / 9


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9 / 5 + 32


def round_temperature(value: float, method: RoundingMethod, precision: int = 0) -> float:
    quantum = Decimal(1).scaleb(-precision)
    modes = {
        RoundingMethod.HALF_UP: ROUND_HALF_UP,
        RoundingMethod.FLOOR: ROUND_FLOOR,
        RoundingMethod.CEILING: ROUND_CEILING,
    }
    rounded = Decimal(str(value)).quantize(quantum, rounding=modes[method])
    return float(rounded)


def daily_maximum(
    points: Sequence[ForecastPoint], market_date: object, timezone: str
) -> float | None:
    values = [
        point.temperature
        for point in points
        if point.temperature is not None
        and point.timestamp.astimezone(ZoneInfo(timezone)).date() == market_date
    ]
    return max(values) if values else None


def calculate_probabilities(
    members: Sequence[MemberDailyValue],
    buckets: Sequence[Bucket],
    *,
    rounding_method: RoundingMethod,
    unit: TemperatureUnit,
    model_weights: Mapping[str, float],
) -> ProbabilityResult:
    del unit
    valid_by_model: dict[str, list[MemberDailyValue]] = defaultdict(list)
    excluded = 0
    for member in members:
        if member.value is None or member.exclusion_reason:
            excluded += 1
        else:
            valid_by_model[member.model].append(member)

    if not valid_by_model:
        return ProbabilityResult(
            valid_members=0,
            excluded_members=excluded,
            outcome_probabilities={bucket.label: 0.0 for bucket in buckets},
            ensemble_spread=0.0,
            uncertainty_score=1.0,
            model_weights={},
        )

    weights = {model: float(model_weights.get(model, 1.0)) for model in valid_by_model}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("model weights must have a positive sum")
    weights = {model: weight / total_weight for model, weight in weights.items()}

    probabilities = {bucket.label: 0.0 for bucket in buckets}
    mapped_values: list[float] = []
    valid_count = 0
    for model, model_members in valid_by_model.items():
        member_weight = weights[model] / len(model_members)
        for member in model_members:
            assert member.value is not None
            corrected = member.value + member.bias_correction
            rounded = round_temperature(corrected, rounding_method)
            bucket = next((candidate for candidate in buckets if candidate.contains(rounded)), None)
            if bucket is None:
                excluded += 1
                continue
            probabilities[bucket.label] += member_weight
            mapped_values.append(corrected)
            valid_count += 1

    probability_total = sum(probabilities.values())
    if probability_total:
        probabilities = {label: value / probability_total for label, value in probabilities.items()}
    spread = pstdev(mapped_values) if len(mapped_values) > 1 else 0.0
    uncertainty = 1.0 - max(probabilities.values(), default=0.0)
    return ProbabilityResult(
        valid_members=valid_count,
        excluded_members=excluded,
        outcome_probabilities=probabilities,
        ensemble_spread=spread,
        uncertainty_score=uncertainty,
        model_weights=weights,
    )
