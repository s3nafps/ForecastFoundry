from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation


class CryptoDataQualityError(ValueError):
    pass


@dataclass(frozen=True)
class CryptoCandle:
    timestamp: datetime
    close: Decimal


@dataclass(frozen=True)
class CryptoSeries:
    source: str
    candles: tuple[CryptoCandle, ...]
    log_returns: tuple[Decimal, ...]
    latest_at: datetime
    quality_flags: tuple[str, ...] = ()


def normalize_candles(
    source: str,
    rows: Iterable[CryptoCandle | Mapping[str, object]],
    *,
    now: datetime,
    freshness: timedelta = timedelta(hours=2),
    min_history: int = 3,
) -> CryptoSeries:
    if not source.strip():
        raise CryptoDataQualityError("source_missing")
    if now.tzinfo is None:
        raise CryptoDataQualityError("now_must_be_timezone_aware")

    candles = tuple(_coerce_candle(row) for row in rows)
    if not candles:
        raise CryptoDataQualityError("insufficient_history")
    timestamps = tuple(candle.timestamp for candle in candles)
    if len(set(timestamps)) != len(timestamps):
        raise CryptoDataQualityError("duplicate_candle")
    ordered = tuple(sorted(candles, key=lambda candle: candle.timestamp))
    latest_at = ordered[-1].timestamp
    if now.astimezone(UTC) - latest_at > freshness:
        raise CryptoDataQualityError("stale_data")
    if len(ordered) < min_history:
        raise CryptoDataQualityError("insufficient_history")

    flags = ("out_of_order_sorted",) if timestamps != tuple(sorted(timestamps)) else ()
    returns = tuple(
        (current.close / previous.close).ln()
        for previous, current in zip(ordered, ordered[1:], strict=False)
    )
    return CryptoSeries(
        source=source,
        candles=ordered,
        log_returns=returns,
        latest_at=latest_at,
        quality_flags=flags,
    )


def _coerce_candle(row: CryptoCandle | Mapping[str, object]) -> CryptoCandle:
    if isinstance(row, CryptoCandle):
        candle = row
    else:
        try:
            timestamp_value = row["timestamp"]
            close_value = row["close"]
            timestamp = _coerce_timestamp(timestamp_value)
            close = Decimal(str(close_value))
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            raise CryptoDataQualityError("invalid_candle") from exc
        candle = CryptoCandle(timestamp=timestamp, close=close)
    if candle.timestamp.tzinfo is None:
        raise CryptoDataQualityError("timestamp_must_be_timezone_aware")
    if not candle.close.is_finite() or candle.close <= 0:
        raise CryptoDataQualityError("invalid_close")
    return CryptoCandle(candle.timestamp.astimezone(UTC), candle.close)


def _coerce_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("timestamp must be an ISO string or datetime")
