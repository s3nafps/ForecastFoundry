from collections.abc import Mapping
from datetime import date, datetime
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
    event_id: str
    location_name: str
    latitude: float
    longitude: float
    station_id: str
    local_date: date
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
        overrides: Mapping[str, Mapping[str, object]] | None = None,
        allow_legacy_unresolved: bool = False,
    ) -> None:
        self._stations = dict(stations or _load_default_stations())
        self._overrides = dict(overrides or {})
        self._allow_legacy_unresolved = allow_legacy_unresolved

    def matches(self, market: MarketInput) -> bool:
        text = f"{market.title} {market.description}".lower()
        if "rainfall" in text or "precipitation" in text:
            return False
        return any(term in text for term in ("temperature", "°c", "°f", "â°c", "â°f"))

    def normalize(self, market: MarketInput) -> DomainRoute:
        if self._allow_legacy_unresolved and not market.raw_data:
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
            expiry = _expiry_for_market(market.market_id, event)
            normalized = normalize_temperature_event(
                event, self._stations, self._overrides.get(event.id)
            )
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
            contract=_contract_from_normalized(market.market_id, normalized, expiry),
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


def _expiry_for_market(market_id: str, event: GammaEvent) -> datetime:
    if market_id == event.id:
        expiry = event.end_date
    else:
        source_market = next((item for item in event.markets if item.id == market_id), None)
        if source_market is None:
            raise RuleNormalizationError("market id not in supplied event")
        expiry = source_market.end_date
    if expiry is None:
        raise RuleNormalizationError("expiry missing")
    return expiry


def _contract_from_normalized(
    market_id: str, normalized: NormalizedEvent, expiry: datetime | None
) -> WeatherContract:
    return WeatherContract(
        market_id=market_id,
        domain="weather",
        resolution_source=normalized.resolution_source,
        time_semantics=normalized.reporting_period,
        unit_or_quote=normalized.unit.value,
        expiry=expiry,
        provenance=normalized.field_provenance,
        event_id=normalized.event_id,
        location_name=normalized.location_name,
        latitude=normalized.latitude,
        longitude=normalized.longitude,
        station_id=normalized.station_id,
        local_date=normalized.local_date,
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
