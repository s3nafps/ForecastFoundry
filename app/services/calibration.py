from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class CalibrationBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lower: Decimal
    upper: Decimal
    count: int
    mean_predicted: Decimal | None = None
    observed_frequency: Decimal | None = None


class CalibrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brier_score: Decimal
    buckets: tuple[CalibrationBucket, ...]


def brier_score(probabilities: Iterable[Decimal], outcomes: Iterable[bool]) -> Decimal:
    values = tuple(probabilities)
    actuals = tuple(outcomes)
    _validate_inputs(values, actuals)
    total = sum(
        (probability - Decimal(int(outcome))) ** 2
        for probability, outcome in zip(values, actuals, strict=True)
    )
    return total / Decimal(len(values))


def walk_forward_calibration(
    probabilities: Iterable[Decimal], outcomes: Iterable[bool], *, bins: int = 10
) -> CalibrationReport:
    values = tuple(probabilities)
    actuals = tuple(outcomes)
    _validate_inputs(values, actuals)
    if bins <= 0:
        raise ValueError("bins must be positive")
    buckets: list[CalibrationBucket] = []
    for index in range(bins):
        lower = Decimal(index) / Decimal(bins)
        upper = Decimal(index + 1) / Decimal(bins)
        selected = tuple(
            (probability, outcome)
            for probability, outcome in zip(values, actuals, strict=True)
            if lower <= probability < upper or (index == bins - 1 and probability == upper)
        )
        buckets.append(
            CalibrationBucket(
                lower=lower,
                upper=upper,
                count=len(selected),
                mean_predicted=(
                    sum((p for p, _ in selected), Decimal("0")) / Decimal(len(selected))
                )
                if selected
                else None,
                observed_frequency=(
                    Decimal(sum(int(o) for _, o in selected)) / Decimal(len(selected))
                )
                if selected
                else None,
            )
        )
    return CalibrationReport(brier_score=brier_score(values, actuals), buckets=tuple(buckets))


def _validate_inputs(probabilities: tuple[Decimal, ...], outcomes: tuple[bool, ...]) -> None:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities and outcomes must have the same length")
    if any(probability < 0 or probability > 1 for probability in probabilities):
        raise ValueError("probability must be between 0 and 1")
