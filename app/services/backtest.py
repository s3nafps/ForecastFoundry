"""Small, deterministic, chronological paper backtest engine."""

import csv
import hashlib
import json
import math
import random
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any


class BacktestError(ValueError):
    pass


def run_temporal_backtest(dataset: str) -> dict[str, object]:
    path = Path(dataset)
    raw = path.read_bytes()
    records = _load_records(path, raw)
    if len(records) < 4:
        raise BacktestError("at least four timestamped records are required")
    normalized = [_normalize(record) for record in records]
    normalized.sort(key=lambda row: row["timestamp"])
    timestamps = [row["timestamp"] for row in normalized]
    if len(set(timestamps)) != len(timestamps):
        raise BacktestError("duplicate timestamps are not permitted")
    for row in normalized:
        feature_time = row.get("feature_timestamp")
        if feature_time and feature_time > row["timestamp"]:
            raise BacktestError("temporal leakage: feature timestamp is after prediction time")

    split = max(2, int(len(normalized) * 0.6))
    if split >= len(normalized):
        split = len(normalized) - 1
    train = normalized[:split]
    evaluation = normalized[split:]
    baseline_probability = mean(row["outcome"] for row in train)
    model_probabilities = [row["probability"] for row in evaluation]
    outcomes = [row["outcome"] for row in evaluation]
    model_brier = mean(
        (prediction - outcome) ** 2
        for prediction, outcome in zip(model_probabilities, outcomes, strict=True)
    )
    baseline_brier = mean((baseline_probability - outcome) ** 2 for outcome in outcomes)
    model_log_loss = mean(
        _log_loss(prediction, outcome)
        for prediction, outcome in zip(model_probabilities, outcomes, strict=True)
    )
    baseline_log_loss = mean(_log_loss(baseline_probability, outcome) for outcome in outcomes)
    pnl = 0.0
    turnover = 0.0
    trades = 0
    for row in evaluation:
        price = row.get("price", 0.5)
        if abs(row["probability"] - price) < row.get("buffer", 0.05):
            continue
        cost = price + row.get("fee", 0.0) + row.get("slippage", 0.0)
        if not 0 < cost < 1:
            continue
        pnl += row["outcome"] - cost
        turnover += cost
        trades += 1
    drawdown = _max_drawdown([pnl])
    ci_low, ci_high = _bootstrap_ci(model_probabilities, outcomes)
    return {
        "status": "completed",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "records": len(normalized),
        "train_records": len(train),
        "evaluation_records": len(evaluation),
        "metrics": {
            "brier_score": model_brier,
            "baseline_brier_score": baseline_brier,
            "log_loss": model_log_loss,
            "baseline_log_loss": baseline_log_loss,
            "calibration_error": abs(mean(model_probabilities) - mean(outcomes)),
            "sharpness": mean(abs(prediction - 0.5) for prediction in model_probabilities),
            "pnl": pnl,
            "max_drawdown": drawdown,
            "turnover": turnover,
            "trades": trades,
            "rejected": len(evaluation) - trades,
            "brier_ci_95": {"low": ci_low, "high": ci_high},
        },
        "promotion": {
            "baseline": "historical_rate",
            "model_promoted": model_brier <= baseline_brier,
            "rule": "model brier score must not exceed baseline",
        },
        "split": "expanding_chronological",
    }


def _load_records(path: Path, raw: bytes) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        return [dict(row) for row in csv.DictReader(raw.decode("utf-8").splitlines())]
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise BacktestError("dataset must be a JSON array or an object with records")
    return payload


def _normalize(record: dict[str, Any]) -> dict[str, Any]:
    timestamp_value = record.get("timestamp")
    if not timestamp_value:
        raise BacktestError("timestamp is required")
    timestamp = _parse_timestamp(str(timestamp_value))
    probability = float(record.get("probability", record.get("prediction", -1)))
    outcome = float(record.get("outcome", -1))
    if not 0 <= probability <= 1 or outcome not in {0.0, 1.0}:
        raise BacktestError("probability must be in [0, 1] and outcome must be 0 or 1")
    feature_timestamp = record.get("feature_timestamp")
    return {
        "timestamp": timestamp,
        "feature_timestamp": _parse_timestamp(str(feature_timestamp))
        if feature_timestamp
        else None,
        "probability": probability,
        "outcome": outcome,
        "price": float(record.get("price", 0.5)),
        "fee": float(record.get("fee", 0.0)),
        "slippage": float(record.get("slippage", 0.0)),
        "buffer": float(record.get("buffer", 0.05)),
    }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BacktestError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise BacktestError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _log_loss(probability: float, outcome: float) -> float:
    probability = min(max(probability, 1e-12), 1 - 1e-12)
    return -(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability))


def _max_drawdown(pnl: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _bootstrap_ci(probabilities: list[float], outcomes: list[float]) -> tuple[float, float]:
    randomizer = random.Random(17)
    samples: list[float] = []
    pairs = list(zip(probabilities, outcomes, strict=True))
    for _ in range(200):
        sample = [randomizer.choice(pairs) for _ in pairs]
        samples.append(mean((prediction - outcome) ** 2 for prediction, outcome in sample))
    samples.sort()
    return samples[5], samples[194]
