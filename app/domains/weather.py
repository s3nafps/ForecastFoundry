from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.domains.base import DomainRoute, MarketInput, NormalizedMarket
from app.schemas import GammaEvent, NormalizedEvent, Station
from app.services.rules import (
    RuleNormalizationError,
    load_station_registry,
    normalize_temperature_event,
)


class WeatherContract(NormalizedMarket):
    location_name: str
    station_id: str
    timezone: str
    measurement: str
    rounding_method: str
    reporting_period: str
    buckets: tuple[dict[str, object], ...]
    confidence_score: int
    original_rules: str


class WeatherPlugin:
    name = "weather"

    def __init__(
        self,
        *,
        stations: Mapping[str, Station] | None = None,
        strict: bool = False,
    ) -> None:
        self._stations = dict(stations or _load_default_stations())
        self._strict = strict

    def matches(self, market: MarketInput) -> bool:
        text = f"{market.title} {market.description}".lower()
        return any(
            term in text
            for term in ("temperature", "°c", "°f", "â°c", "â°f", "rainfall", "precipitation")
        )

    def normalize(self, market: MarketInput) -> DomainRoute:
        if not self._strict and not market.raw_data:
            return DomainRoute(
                accepted=True,
                domain=self.name,
                contract=NormalizedMarket(
                    market_id=market.market_id,
                    domain=self.name,
                    resolution_source="legacy-unresolved",
                    time_semantics="requires detailed source contract",
                    unit_or_quote="weather measurement",
                    provenance={"title": "polymarket", "description": "polymarket"},
                    ambiguities=("weather_contract_unresolved",),
                ),
            )
        try:
            event = _event_from_market(market)
            normalized = normalize_temperature_event(event, self._stations)
        except (RuleNormalizationError, ValueError, TypeError) as exc:
            return DomainRoute(
                accepted=False,
                domain=self.name,
                reasons=(f"weather_contract_invalid:{_reason_code(str(exc))}",),
            )
        if normalized.ambiguities:
            return DomainRoute(
                accepted=False,
                domain=self.name,
                reasons=tuple(f"weather_ambiguous:{item}" for item in normalized.ambiguities),
            )
        return DomainRoute(
            accepted=True,
            domain=self.name,
            contract=_contract_from_normalized(market.market_id, normalized),
        )


def _load_default_stations() -> dict[str, Station]:
    path = Path("config/stations.yaml")
    return load_station_registry(path) if path.is_file() else {}


def _event_from_market(market: MarketInput) -> GammaEvent:
    raw: dict[str, Any] = dict(market.raw_data)
    event_payload = raw.get("event", raw.get("gamma_event", raw))
    if not isinstance(event_payload, dict):
        raise RuleNormalizationError("event payload is missing")
    return GammaEvent.model_validate(event_payload)


def _contract_from_normalized(market_id: str, normalized: NormalizedEvent) -> WeatherContract:
    return WeatherContract(
        market_id=market_id,
        domain="weather",
        resolution_source=normalized.resolution_source,
        time_semantics=normalized.reporting_period,
        unit_or_quote=normalized.unit.value,
        expiry=None,
        provenance=normalized.field_provenance,
        location_name=normalized.location_name,
        station_id=normalized.station_id,
        timezone=normalized.timezone,
        measurement=normalized.measurement,
        rounding_method=normalized.rounding_method.value,
        reporting_period=normalized.reporting_period,
        buckets=tuple(bucket.model_dump(mode="json") for bucket in normalized.buckets),
        confidence_score=normalized.confidence_score,
        original_rules=normalized.original_rules,
    )


def _reason_code(message: str) -> str:
    return "_".join(message.lower().replace("'", "").split())[:120]
