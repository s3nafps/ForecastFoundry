from decimal import Decimal

import pytest

from app.database import make_engine, make_session_factory
from app.models import ApplicationSetting, Base
from app.services.model_weights import (
    WEIGHTS_KEY,
    ModelSample,
    compute_weights,
    extract_samples,
    load_model_weights,
    model_brier,
)


def _samples(rows: dict[str, list[tuple[str, bool]]]) -> dict[str, list[ModelSample]]:
    out: dict[str, list[ModelSample]] = {}
    for model, entries in rows.items():
        out[model] = [
            ModelSample(model=model, probability=Decimal(probability), won=won)
            for probability, won in entries
        ]
    return out


def test_extract_samples_reads_signal_data_snapshots() -> None:
    signals = [
        ({"model_probabilities": {"gfs_seamless": "0.6", "ecmwf_ifs025": "0.4"}}, True),
        ({"model_probabilities": {"gfs_seamless": "0.8", "ecmwf_ifs025": "0.2"}}, False),
        ({"event_id": "no-snapshot"}, True),
    ]

    samples = extract_samples(signals)

    assert set(samples) == {"gfs_seamless", "ecmwf_ifs025"}
    assert len(samples["gfs_seamless"]) == 2


def test_model_brier_matches_manual_calculation() -> None:
    samples = _samples({"gfs_seamless": [("0.5", True), ("0.5", False)]})

    brier = model_brier(samples)["gfs_seamless"]

    assert brier == Decimal("0.25")


def test_compute_weights_returns_none_below_min_samples() -> None:
    samples = _samples({"gfs_seamless": [("0.6", True)] * 5})

    assert compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05")) is None


def test_compute_weights_returns_none_when_improvement_is_small() -> None:
    samples = _samples(
        {
            "gfs_seamless": [("0.5", True), ("0.5", False)] * 20,
            "ecmwf_ifs025": [("0.5", True), ("0.5", False)] * 20,
        }
    )

    assert compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05")) is None


def test_compute_weights_promotes_better_model() -> None:
    samples = _samples(
        {
            "gfs_seamless": [("0.9", True), ("0.9", False)] * 20,
            "ecmwf_ifs025": [("0.55", True), ("0.55", False)] * 20,
        }
    )

    weights = compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05"))

    assert weights is not None
    assert weights["ecmwf_ifs025"] > weights["gfs_seamless"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_compute_weights_handles_perfect_model() -> None:
    samples = _samples(
        {
            "A": [("1.0", True)] * 30,
            "B": [("0.5", True), ("0.5", False)] * 15,
        }
    )

    weights = compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05"))

    assert weights == {"A": 1.0, "B": 0.0}


def test_compute_weights_returns_none_for_perfect_model_below_min_samples() -> None:
    samples = _samples({"A": [("1.0", True)] * 5})

    assert compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05")) is None


async def test_load_weights_falls_back_on_corrupt_stored_value() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        session.add(ApplicationSetting(key=WEIGHTS_KEY, value={"gfs_seamless": "not-a-number"}))
        await session.commit()

    async with sessions() as session:
        weights = await load_model_weights(session)

    assert weights == {}


@pytest.mark.parametrize("stored", ["nan", "inf", "1.5"])
async def test_load_weights_rejects_non_finite_and_out_of_range_values(stored: str) -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        session.add(ApplicationSetting(key=WEIGHTS_KEY, value={"gfs_seamless": stored}))
        await session.commit()

    async with sessions() as session:
        weights = await load_model_weights(session)

    assert weights == {}


async def test_load_weights_keeps_valid_entries_next_to_corrupt_ones() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        session.add(
            ApplicationSetting(
                key=WEIGHTS_KEY,
                value={"gfs_seamless": "0.7", "ecmwf_ifs025": "not-a-number"},
            )
        )
        await session.commit()

    async with sessions() as session:
        weights = await load_model_weights(session)

    assert weights == {"gfs_seamless": 0.7}
