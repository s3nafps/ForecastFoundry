from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.services.http import ResilientHttpClient


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


class CryptoMarketDataClient:
    """Public, keyless candle client with a short freshness cache."""

    def __init__(self, http: ResilientHttpClient) -> None:
        self.http = http
        self._cache: dict[tuple[str, str, str], tuple[datetime, CryptoSeries]] = {}

    async def fetch_series(
        self,
        source: str,
        *,
        asset: str,
        quote: str,
        now: datetime,
        granularity: str = "1h",
        limit: int = 200,
    ) -> CryptoSeries:
        key = (source, f"{asset}-{quote}", granularity)
        cached = self._cache.get(key)
        if cached and (now - cached[0]).total_seconds() < 30:
            return cached[1]
        url, params = _endpoint(source, asset, quote, granularity, limit)
        payload = await self.http.request_json("GET", url, params=params)
        rows = _rows(source, payload)
        series = normalize_candles(source, rows, now=now)
        self._cache[key] = (now, series)
        return series


def _endpoint(
    source: str, asset: str, quote: str, granularity: str, limit: int
) -> tuple[str, dict[str, object]]:
    pair = f"{asset.upper()}-{quote.upper()}"
    if source == "coinbase":
        return (
            f"https://api.exchange.coinbase.com/products/{pair}/candles",
            {"granularity": 3600 if granularity == "1h" else 86400},
        )
    if source == "binance":
        return (
            "https://api.binance.com/api/v3/klines",
            {"symbol": f"{asset.upper()}{quote.upper()}", "interval": granularity, "limit": limit},
        )
    if source == "kraken":
        kraken_pair = "XBT" if asset.upper() == "BTC" else asset.upper()
        return (
            "https://api.kraken.com/0/public/OHLC",
            {"pair": f"{kraken_pair}{quote.upper()}", "interval": 60},
        )
    raise ValueError(f"unsupported public crypto source: {source}")


def _rows(source: str, payload: object) -> list[dict[str, object]]:
    raw_rows: object = payload
    if source == "kraken":
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise CryptoDataQualityError("invalid_provider_payload")
        payload_result = payload["result"]
        raw_rows = next((value for key, value in payload_result.items() if key != "last"), [])
    if not isinstance(raw_rows, list):
        raise CryptoDataQualityError("invalid_provider_payload")
    normalized_rows: list[dict[str, object]] = []
    for row in raw_rows:
        if source == "coinbase" and isinstance(row, list) and len(row) >= 5:
            normalized_rows.append(
                {"timestamp": datetime.fromtimestamp(float(row[0]), UTC), "close": row[4]}
            )
        elif source == "binance" and isinstance(row, list) and len(row) >= 5:
            normalized_rows.append(
                {
                    "timestamp": datetime.fromtimestamp(float(row[0]) / 1000, UTC),
                    "close": row[4],
                }
            )
        elif source == "kraken" and isinstance(row, list) and len(row) >= 5:
            normalized_rows.append(
                {"timestamp": datetime.fromtimestamp(float(row[0]), UTC), "close": row[4]}
            )
        else:
            raise CryptoDataQualityError("invalid_candle")
    return normalized_rows


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
