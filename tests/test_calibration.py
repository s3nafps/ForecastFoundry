from decimal import Decimal

import pytest

from app.services.calibration import brier_score, walk_forward_calibration


def test_brier_score_and_reliability_buckets_are_deterministic() -> None:
    report = walk_forward_calibration(
        (Decimal("0.1"), Decimal("0.4"), Decimal("0.9"), Decimal("0.8")),
        (False, False, True, True),
        bins=2,
    )

    assert report.brier_score == Decimal("0.055")
    assert report.buckets[0].count == 2
    assert report.buckets[1].count == 2


def test_calibration_rejects_mismatched_or_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="same length"):
        walk_forward_calibration((Decimal("0.5"),), (True, False))
    with pytest.raises(ValueError, match="probability"):
        brier_score((Decimal("1.2"),), (True,))
