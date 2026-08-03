import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.domains.base import DomainRoute, MarketInput, NormalizedMarket


class CryptoContract(NormalizedMarket):
    model_config = ConfigDict(extra="forbid")

    asset: str
    quote: str
    source: str
    comparison: str
    threshold: Decimal | None = None
    comparison_reference_price: Decimal | None = None
    comparison_reference_time: datetime | None = None
    price_definition: str
    timezone: str = "UTC"
    rounding: str
    original_rules: dict[str, object] = Field(default_factory=dict)


class CryptoMarketResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    contract: CryptoContract | None = None
    reasons: tuple[str, ...] = ()


class CryptoPlugin:
    name = "crypto"
    _assets = re.compile(r"\b(?:btc|bitcoin|eth|ethereum)\b", re.IGNORECASE)
    _comparisons = re.compile(r"\b(?:above|below|up|down)\b", re.IGNORECASE)
    _sources = re.compile(
        r"\b(?:coinbase|binance|kraken|chainlink|pyth|cf\s+benchmarks)\b", re.IGNORECASE
    )
    _unsupported_assets = re.compile(
        r"\b(?:sol|solana|xrp|ripple|doge|dogecoin|ada|cardano)\b", re.IGNORECASE
    )
    _source_names: ClassVar[dict[str, str]] = {
        "coinbase": "coinbase",
        "binance": "binance",
        "kraken": "kraken",
        "chainlink": "chainlink",
        "pyth": "pyth",
        "cf benchmarks": "cf_benchmarks",
    }

    def matches(self, market: MarketInput) -> bool:
        text = f"{market.title} {market.description}"
        return bool(
            (self._assets.search(text) or self._unsupported_assets.search(text))
            and (self._comparisons.search(text) or self._sources.search(text))
        )

    def normalize(self, market: MarketInput) -> DomainRoute:
        result = parse_crypto_market(market)
        return DomainRoute(
            accepted=result.accepted,
            domain=self.name,
            contract=result.contract,
            reasons=result.reasons,
        )


def parse_crypto_market(market: MarketInput) -> CryptoMarketResult:
    text = f"{market.title} {market.description}"
    lowered = text.lower()
    reasons: list[str] = []

    asset_matches = re.findall(r"\b(btc|bitcoin|eth|ethereum)\b", text, re.IGNORECASE)
    unsupported = bool(CryptoPlugin._unsupported_assets.search(text))
    if not asset_matches:
        reasons.append("unsupported_asset" if unsupported else "asset_missing")
        asset = ""
    else:
        asset = "BTC" if asset_matches[0].lower() in {"btc", "bitcoin"} else "ETH"

    quotes = tuple(dict.fromkeys(re.findall(r"\b(?:USD|USDT|USDC|EUR)\b", text, re.IGNORECASE)))
    if not quotes:
        reasons.append("quote_missing")
        quote = ""
    elif len(quotes) > 1:
        reasons.append("quote_ambiguous")
        quote = ""
    else:
        quote = quotes[0].upper()

    found_sources = tuple(
        name
        for marker, name in CryptoPlugin._source_names.items()
        if re.search(rf"\b{marker}\b", lowered)
    )
    if not found_sources:
        reasons.append("resolution_source_missing")
        source = ""
    elif len(found_sources) > 1:
        reasons.append("resolution_source_ambiguous")
        source = ""
    else:
        source = found_sources[0]

    comparison_match = re.search(r"\b(above|below|up|down)\b", lowered)
    comparison = comparison_match.group(1) if comparison_match else ""
    threshold: Decimal | None = None
    comparison_reference_price: Decimal | None = None
    comparison_reference_time: datetime | None = None
    if not comparison:
        reasons.append("comparison_missing")
    elif comparison in {"above", "below"}:
        threshold_match = re.search(r"(?:above|below)\s+\$?([0-9][0-9,]*(?:\.\d+)?)", lowered)
        if not threshold_match:
            reasons.append("threshold_missing")
        else:
            try:
                threshold = Decimal(threshold_match.group(1).replace(",", ""))
            except InvalidOperation:
                reasons.append("threshold_invalid")
    else:
        comparison_reference_price, comparison_reference_time = _parse_comparison_reference(text)
        if comparison_reference_price is None or comparison_reference_time is None:
            reasons.append("comparison_baseline_missing")

    expiry = _parse_expiry(text)
    if expiry is None:
        reasons.append("expiry_missing")

    price_definition = _find_price_definition(lowered)
    if not price_definition:
        reasons.append("price_definition_missing")

    rounding = _find_rounding(lowered)
    if not rounding:
        reasons.append("rounding_definition_missing")

    if reasons:
        return CryptoMarketResult(accepted=False, reasons=tuple(dict.fromkeys(reasons)))

    assert expiry is not None
    assert price_definition is not None
    assert rounding is not None
    contract = CryptoContract(
        market_id=market.market_id,
        domain="crypto",
        resolution_source=source,
        time_semantics="UTC expiry",
        unit_or_quote=quote,
        expiry=expiry,
        provenance={"title": "polymarket", "description": "polymarket"},
        asset=asset,
        quote=quote,
        source=source,
        comparison=comparison,
        threshold=threshold,
        comparison_reference_price=comparison_reference_price,
        comparison_reference_time=comparison_reference_time,
        price_definition=price_definition,
        rounding=rounding,
        original_rules={
            "title": market.title,
            "description": market.description,
            "raw_data": dict(market.raw_data),
        },
    )
    return CryptoMarketResult(accepted=True, contract=contract)


def _parse_expiry(text: str) -> datetime | None:
    match = re.search(
        r"\b(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)\s*(UTC|Z)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}+00:00").astimezone(UTC)
    except ValueError:
        return None


def _parse_comparison_reference(text: str) -> tuple[Decimal | None, datetime | None]:
    match = re.search(
        r"\b(?:reference|baseline|starting)\s+price\s+(?:is\s+|of\s+)?"
        r"\$?([0-9][0-9,]*(?:\.\d+)?)\s+(?:at|as of)\s+"
        r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)\s*(?:UTC|Z)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    try:
        price = Decimal(match.group(1).replace(",", ""))
        timestamp = datetime.fromisoformat(f"{match.group(2)}T{match.group(3)}+00:00").astimezone(
            UTC
        )
    except (InvalidOperation, ValueError):
        return None, None
    return price, timestamp


def _find_price_definition(text: str) -> str | None:
    for phrase in ("closing price", "last price", "index level", "close"):
        if re.search(rf"\b{phrase}\b", text):
            return phrase
    return None


def _find_rounding(text: str) -> str | None:
    match = re.search(
        r"\b(?:rounded?|rounding)\s+to\s+(?:the\s+)?(?:nearest\s+)?([a-z0-9 .-]+?)(?:[,.]|$)",
        text,
    )
    if match:
        return f"rounded to {match.group(1).strip()}"
    if re.search(r"\b(?:nearest|cents?|decimals?)\b", text):
        return "explicit rounding"
    return None
