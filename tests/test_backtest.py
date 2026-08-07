import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.backtest import BacktestError, run_temporal_backtest


def _dataset(path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    path.write_text(
        json.dumps(
            [
                {
                    "timestamp": (start + timedelta(days=index)).isoformat(),
                    "feature_timestamp": (start + timedelta(days=index - 1)).isoformat(),
                    "probability": 0.8 if index % 2 else 0.2,
                    "outcome": 1 if index % 2 else 0,
                    "price": 0.5,
                }
                for index in range(6)
            ]
        ),
        encoding="utf-8",
    )


def test_temporal_backtest_reports_metrics_and_baseline(tmp_path: Path) -> None:
    path = tmp_path / "dataset.json"
    _dataset(path)
    result = run_temporal_backtest(str(path))
    assert result["status"] == "completed"
    metrics = result["metrics"]
    assert result["evaluation_records"] == 3
    assert "brier_score" in metrics
    assert "max_drawdown" in metrics
    assert result["promotion"]["baseline"] == "historical_rate"


def test_temporal_backtest_rejects_future_features(tmp_path: Path) -> None:
    path = tmp_path / "leakage.json"
    path.write_text(
        json.dumps(
            [
                {
                    "timestamp": f"2026-01-0{index + 1}T00:00:00+00:00",
                    "feature_timestamp": f"2026-02-0{index + 1}T00:00:00+00:00",
                    "probability": 0.5,
                    "outcome": index % 2,
                }
                for index in range(4)
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(BacktestError, match="leakage"):
        run_temporal_backtest(str(path))
