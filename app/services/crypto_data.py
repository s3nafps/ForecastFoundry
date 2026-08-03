import hashlib
import json
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
    interval_seconds: int
    retrieved_at: datetime
    provider_version: str
    raw_response_hash: str
    request_url: str
    request_params: dict[str, object]
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
        rows = _rows(
            source,
            payload,
            expected_pair=(_kraken_response_pair(asset, quote) if source == "kraken" else None),
        )
        interval = _interval(granularity)
        series = normalize_candles(
            source,
            rows,
            now=now,
            interval=interval,
            provider_version=f"{source}-public-candles-v1",
            raw_response=payload,
            request_url=url,
            request_params=params,
        )
        self._cache[key] = (now, series)
        return series


def _endpoint(
    source: str, asset: str, quote: str, granularity: str, limit: int
) -> tuple[str, dict[str, object]]:
    normalized_asset = asset.upper()
    normalized_quote = quote.upper()
    if normalized_asset not in {"BTC", "ETH"}:
        raise ValueError("unsupported_pair")
    seconds = int(_interval(granularity).total_seconds())
    if source == "coinbase":
        if normalized_quote not in {"USD", "USDT", "USDC", "EUR"}:
            raise ValueError("unsupported_pair")
        return (
            "https://api.exchange.coinbase.com/products/"
            f"{normalized_asset}-{normalized_quote}/candles",
            {"granularity": seconds},
        )
    if source == "binance":
        if normalized_quote not in {"USDT", "USDC", "EUR"}:
            raise ValueError("unsupported_pair")
        return (
            "https://api.binance.com/api/v3/klines",
            {
                "symbol": f"{normalized_asset}{normalized_quote}",
                "interval": granularity,
                "limit": limit,
            },
        )
    if source == "kraken":
        if normalized_quote not in {"USD", "USDT", "USDC", "EUR"}:
            raise ValueError("unsupported_pair")
        kraken_pair = "XBT" if normalized_asset == "BTC" else normalized_asset
        return (
            "https://api.kraken.com/0/public/OHLC",
            {"pair": f"{kraken_pair}{normalized_quote}", "interval": seconds // 60},
        )
    raise ValueError(f"unsupported public crypto source: {source}")


def _rows(
    source: str, payload: object, *, expected_pair: str | None = None
) -> list[dict[str, object]]:
    raw_rows: object = payload
    if source == "kraken":
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise CryptoDataQualityError("invalid_provider_payload")
        errors = payload.get("error")
        if not isinstance(errors, list) or errors:
            raise CryptoDataQualityError("provider_error")
        payload_result = payload["result"]
        pair_keys = [key for key in payload_result if key != "last"]
        if len(pair_keys) != 1:
            raise CryptoDataQualityError("provider_pair_ambiguous")
        if expected_pair is None or pair_keys[0] != expected_pair:
            raise CryptoDataQualityError("provider_pair_mismatch")
        raw_rows = payload_result[pair_keys[0]]
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
    interval: timedelta = timedelta(hours=1),
    freshness: timedelta = timedelta(hours=2),
    min_history: int = 3,
    provider_version: str | None = None,
    raw_response: object | None = None,
    request_url: str = "normalized-fixture",
    request_params: Mapping[str, object] | None = None,
) -> CryptoSeries:
    if not source.strip():
        raise CryptoDataQualityError("source_missing")
    if now.tzinfo is None:
        raise CryptoDataQualityError("now_must_be_timezone_aware")
    if interval <= timedelta(0):
        raise CryptoDataQualityError("invalid_interval")

    candles = tuple(_coerce_candle(row) for row in rows)
    if not candles:
        raise CryptoDataQualityError("insufficient_history")
    timestamps = tuple(candle.timestamp for candle in candles)
    if len(set(timestamps)) != len(timestamps):
        raise CryptoDataQualityError("duplicate_candle")
    ascending = tuple(sorted(timestamps))
    descending = tuple(reversed(ascending))
    flags: list[str] = []
    if timestamps == ascending:
        ordered = candles
    elif source == "coinbase" and timestamps == descending:
        ordered = tuple(reversed(candles))
        flags.append("source_descending_reversed")
    else:
        raise CryptoDataQualityError("out_of_order_candles")
    interval_seconds = int(interval.total_seconds())
    if any(
        candle.timestamp.microsecond or int(candle.timestamp.timestamp()) % interval_seconds
        for candle in ordered
    ):
        raise CryptoDataQualityError("candle_misaligned")
    if any(
        current.timestamp - previous.timestamp != interval
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        raise CryptoDataQualityError("candle_gap")
    current = now.astimezone(UTC)
    if any(candle.timestamp > current for candle in ordered):
        raise CryptoDataQualityError("future_candle")
    complete = tuple(candle for candle in ordered if candle.timestamp + interval <= current)
    if len(complete) != len(ordered):
        flags.append("incomplete_current_candle_removed")
    if len(complete) < min_history:
        raise CryptoDataQualityError("insufficient_history")
    latest_at = complete[-1].timestamp + interval
    if current - latest_at > freshness:
        raise CryptoDataQualityError("stale_data")

    returns = tuple(
        (current.close / previous.close).ln()
        for previous, current in zip(complete, complete[1:], strict=False)
    )
    normalized_payload = {
        "source": source,
        "interval_seconds": interval_seconds,
        "candles": [
            {"timestamp": candle.timestamp.isoformat(), "close": str(candle.close)}
            for candle in complete
        ],
    }
    return CryptoSeries(
        source=source,
        candles=complete,
        log_returns=returns,
        latest_at=latest_at,
        interval_seconds=interval_seconds,
        retrieved_at=current,
        provider_version=provider_version or f"{source}-normalized-v1",
        raw_response_hash=_hash_payload(
            raw_response if raw_response is not None else normalized_payload
        ),
        request_url=request_url,
        request_params=dict(request_params or {}),
        quality_flags=tuple(flags),
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


def _interval(granularity: str) -> timedelta:
    if granularity == "1h":
        return timedelta(hours=1)
    if granularity == "1d":
        return timedelta(days=1)
    raise ValueError("unsupported_granularity")


def _kraken_response_pair(asset: str, quote: str) -> str:
    bases = {"BTC": "XXBT", "ETH": "XETH"}
    quotes = {"USD": "ZUSD", "EUR": "ZEUR", "USDT": "USDT", "USDC": "USDC"}
    try:
        return f"{bases[asset.upper()]}{quotes[quote.upper()]}"
    except KeyError as exc:
        raise ValueError("unsupported_pair") from exc


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
