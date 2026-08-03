import hashlib
import math
import random
from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Comparison = Literal["above", "below", "up", "down"]


class ProbabilityEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probability: Decimal = Field(ge=0, le=1)
    bootstrap_probability: Decimal = Field(ge=0, le=1)
    monte_carlo_probability: Decimal = Field(ge=0, le=1)
    seed: int
    samples: int
    horizon: int
    input_hash: str
    model_version: str = "crypto-baseline-v1"


def empirical_bootstrap_quantiles(
    returns: Iterable[Decimal],
    quantiles: Iterable[Decimal],
    *,
    seed: int,
    samples: int = 1_000,
    horizon: int = 1,
) -> tuple[Decimal, ...]:
    values = tuple(returns)
    probabilities = tuple(quantiles)
    _validate_model_inputs(values, samples, horizon)
    if any(q < 0 or q > 1 for q in probabilities):
        raise ValueError("quantiles must be between 0 and 1")
    draws = _bootstrap_paths(values, seed=seed, samples=samples, horizon=horizon)
    ordered = sorted(draws)
    return tuple(ordered[int(q * (len(ordered) - 1))] for q in probabilities)


def ewma_volatility(returns: Iterable[Decimal], *, decay: Decimal = Decimal("0.94")) -> Decimal:
    values = tuple(returns)
    if not values:
        raise ValueError("returns must not be empty")
    if not 0 <= decay < 1:
        raise ValueError("decay must be between 0 and 1")
    variance = Decimal("0")
    for value in values:
        variance = decay * variance + (Decimal("1") - decay) * value * value
    return variance.sqrt()


def estimate_crypto_probability(
    returns: Iterable[Decimal],
    *,
    current_price: Decimal,
    comparison: Comparison,
    threshold: Decimal | None,
    horizon: int,
    seed: int,
    samples: int = 5_000,
) -> ProbabilityEstimate:
    values = tuple(returns)
    _validate_model_inputs(values, samples, horizon)
    if comparison in {"above", "below"} and threshold is None:
        raise ValueError("threshold is required for above/below")
    if current_price <= 0:
        raise ValueError("current_price must be positive")

    bootstrap_paths = _bootstrap_paths(values, seed=seed, samples=samples, horizon=horizon)
    bootstrap_probability = _event_probability(
        bootstrap_paths,
        current_price=current_price,
        comparison=comparison,
        threshold=threshold,
    )
    sigma = ewma_volatility(values)
    rng = random.Random(seed + 1)
    monte_carlo_paths = tuple(
        Decimal(str(rng.gauss(0.0, float(sigma) * math.sqrt(horizon)))) for _ in range(samples)
    )
    monte_carlo_probability = _event_probability(
        monte_carlo_paths,
        current_price=current_price,
        comparison=comparison,
        threshold=threshold,
    )
    probability = (bootstrap_probability + monte_carlo_probability) / Decimal("2")
    input_hash = hashlib.sha256(
        "|".join(str(value) for value in values).encode("utf-8")
    ).hexdigest()
    return ProbabilityEstimate(
        probability=probability,
        bootstrap_probability=bootstrap_probability,
        monte_carlo_probability=monte_carlo_probability,
        seed=seed,
        samples=samples,
        horizon=horizon,
        input_hash=input_hash,
    )


def _bootstrap_paths(
    returns: tuple[Decimal, ...], *, seed: int, samples: int, horizon: int
) -> tuple[Decimal, ...]:
    rng = random.Random(seed)
    return tuple(
        sum((returns[rng.randrange(len(returns))] for _ in range(horizon)), Decimal("0"))
        for _ in range(samples)
    )


def _event_probability(
    log_returns: Iterable[Decimal],
    *,
    current_price: Decimal,
    comparison: Comparison,
    threshold: Decimal | None,
) -> Decimal:
    target = threshold if threshold is not None else current_price
    hits = 0
    paths = tuple(log_returns)
    for log_return in paths:
        final_price = current_price * log_return.exp()
        hit = final_price >= target if comparison in {"above", "up"} else final_price <= target
        hits += int(hit)
    return Decimal(hits) / Decimal(len(paths))


def _validate_model_inputs(values: tuple[Decimal, ...], samples: int, horizon: int) -> None:
    if not values:
        raise ValueError("returns must not be empty")
    if samples <= 0 or horizon <= 0:
        raise ValueError("samples and horizon must be positive")
