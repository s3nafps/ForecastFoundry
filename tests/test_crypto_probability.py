from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.crypto_data import CryptoDataQualityError, normalize_candles
from app.services.crypto_probability import (
    empirical_bootstrap_quantiles,
    estimate_crypto_probability,
    ewma_volatility,
)

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def candles(count: int = 5) -> list[dict[str, object]]:
    return [
        {
            "timestamp": NOW - timedelta(hours=count - index - 1),
            "close": Decimal(str(100 + index)),
        }
        for index in range(count)
    ]


def test_normalizes_descending_coinbase_candles_and_returns_log_returns() -> None:
    rows = candles()
    series = normalize_candles("coinbase", tuple(reversed(rows)), now=NOW)

    assert [c.close for c in series.candles] == [
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
        Decimal("103"),
    ]
    assert len(series.log_returns) == 3
    assert series.quality_flags == (
        "source_descending_reversed",
        "incomplete_current_candle_removed",
    )


def test_rejects_duplicate_and_stale_candles() -> None:
    with pytest.raises(CryptoDataQualityError, match="duplicate_candle"):
        normalize_candles("coinbase", [*candles(), candles()[0]], now=NOW)

    stale = [{"timestamp": NOW - timedelta(days=2), "close": Decimal("100")}]
    with pytest.raises(CryptoDataQualityError, match="stale_data"):
        normalize_candles("coinbase", stale, now=NOW, freshness=timedelta(hours=1), min_history=1)


def test_rejects_insufficient_history() -> None:
    with pytest.raises(CryptoDataQualityError, match="insufficient_history"):
        normalize_candles("coinbase", candles(2), now=NOW, min_history=3)


def test_seeded_bootstrap_and_probability_are_reproducible() -> None:
    returns = (Decimal("0.01"), Decimal("-0.005"), Decimal("0.02"))

    quantiles = (Decimal("0.1"), Decimal("0.9"))
    first_quantiles = empirical_bootstrap_quantiles(returns, quantiles, seed=7)
    second_quantiles = empirical_bootstrap_quantiles(returns, quantiles, seed=7)
    assert first_quantiles == second_quantiles
    first = estimate_crypto_probability(
        returns,
        current_price=Decimal("100"),
        comparison="above",
        threshold=Decimal("100"),
        horizon=2,
        seed=7,
        samples=500,
    )
    second = estimate_crypto_probability(
        returns,
        current_price=Decimal("100"),
        comparison="above",
        threshold=Decimal("100"),
        horizon=2,
        seed=7,
        samples=500,
    )
    assert first == second
    assert Decimal("0") <= first.probability <= Decimal("1")


def test_ewma_volatility_shrinks_toward_zero_drift() -> None:
    assert ewma_volatility((Decimal("0"), Decimal("0")), decay=Decimal("0.94")) == Decimal("0")
