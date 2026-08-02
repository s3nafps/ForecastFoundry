import re
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

import yaml

from app.schemas import (
    Bucket,
    GammaEvent,
    NormalizedEvent,
    RoundingMethod,
    Station,
    TemperatureUnit,
)


class RuleNormalizationError(ValueError):
    pass


_NUMBER = r"-?\d+(?:\.\d+)?"
_BUCKET = re.compile(
    rf"^\s*(?P<value>{_NUMBER})\s*°?\s*(?P<unit>[CF])(?:\s+or\s+(?P<range>below|lower|higher|above))?\s*$",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\bon\s+(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3,9})\s+['’]?(?P<year>\d{2,4})\b",
    re.IGNORECASE,
)
_LOCATION = re.compile(r"^Highest temperature in (?P<location>.+?) on ", re.IGNORECASE)
_STATION = re.compile(r"/([A-Z]{4})(?:[/?#]|$)")
_ALLOWED_OVERRIDE_FIELDS = {"resolution_source", "rounding_method", "station_id"}


def load_station_registry(path: Path) -> dict[str, Station]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_stations = payload.get("stations", {})
    if not isinstance(raw_stations, dict):
        raise RuleNormalizationError("station registry must contain a stations mapping")
    stations = {key: Station.model_validate(value) for key, value in raw_stations.items()}
    if any(key != station.station_id for key, station in stations.items()):
        raise RuleNormalizationError("station registry keys must match station_id")
    return stations


def load_market_overrides(path: Path) -> dict[str, dict[str, object]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise RuleNormalizationError("market overrides must be a mapping")
    result: dict[str, dict[str, object]] = {}
    for market_id, raw_override in payload.items():
        if not isinstance(raw_override, dict):
            raise RuleNormalizationError(f"override for {market_id} must be a mapping")
        unknown = set(raw_override) - _ALLOWED_OVERRIDE_FIELDS
        if unknown:
            raise RuleNormalizationError(f"unknown override fields: {sorted(unknown)}")
        result[str(market_id)] = {str(key): value for key, value in raw_override.items()}
    return result


def parse_bucket(label: str, unit: TemperatureUnit) -> Bucket:
    cleaned = label.replace("Â", "")
    match = _BUCKET.fullmatch(cleaned)
    if not match:
        raise RuleNormalizationError(f"unsupported temperature bucket: {label}")
    label_unit = (
        TemperatureUnit.CELSIUS if match["unit"].upper() == "C" else TemperatureUnit.FAHRENHEIT
    )
    if label_unit is not unit:
        raise RuleNormalizationError(f"bucket unit does not match rule unit: {label}")
    value = float(match["value"])
    if value.is_integer():
        value = int(value)
    range_name = match["range"]
    if range_name in {"below", "lower"}:
        return Bucket(label=label, upper=value)
    if range_name in {"higher", "above"}:
        return Bucket(label=label, lower=value)
    return Bucket(label=label, lower=value, upper=value)


def _parse_date(rules: str) -> date:
    match = _DATE.search(rules)
    if not match:
        raise RuleNormalizationError("local date is not explicit in the rules")
    year = int(match["year"])
    if year < 100:
        year += 2000
    try:
        month = datetime.strptime(match["month"][:3].title(), "%b").month
        return date(year, month, int(match["day"]))
    except ValueError as exc:
        raise RuleNormalizationError("local date is invalid") from exc


def _validate_complete_buckets(buckets: tuple[Bucket, ...]) -> tuple[Bucket, ...]:
    ordered = tuple(
        sorted(buckets, key=lambda bucket: float("-inf") if bucket.lower is None else bucket.lower)
    )
    if not ordered or ordered[0].lower is not None or ordered[-1].upper is not None:
        raise RuleNormalizationError("bucket set must include lower and upper terminal ranges")
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.upper is None or current.lower is None:
            raise RuleNormalizationError("temperature buckets overlap")
        if current.lower <= previous.upper:
            raise RuleNormalizationError("temperature buckets overlap")
        if current.lower != previous.upper + 1:
            raise RuleNormalizationError("temperature bucket gap detected")
    return ordered


def normalize_temperature_event(
    event: GammaEvent,
    stations: Mapping[str, Station],
    override: Mapping[str, object] | None = None,
) -> NormalizedEvent:
    rules = event.description.replace("Â", "")
    normalized_rules = " ".join(rules.split())
    if "highest temperature" not in normalized_rules.lower():
        raise RuleNormalizationError("only daily maximum temperature rules are supported")
    location_match = _LOCATION.search(event.title)
    if not location_match:
        raise RuleNormalizationError("location is not explicit in the event title")

    override = override or {}
    source = str(override.get("resolution_source", event.resolution_source))
    station_match = _STATION.search(source)
    station_id = str(override.get("station_id", station_match.group(1) if station_match else ""))
    station = stations.get(station_id)
    if station is None:
        raise RuleNormalizationError(
            f"station {station_id or '<missing>'} is not in the audited registry"
        )

    if "degrees celsius" in normalized_rules.lower():
        unit = TemperatureUnit.CELSIUS
    elif "degrees fahrenheit" in normalized_rules.lower():
        unit = TemperatureUnit.FAHRENHEIT
    else:
        raise RuleNormalizationError("temperature unit is not explicit in the rules")

    ambiguities: tuple[str, ...]
    if "rounding_method" in override:
        try:
            rounding = RoundingMethod(str(override["rounding_method"]))
        except ValueError as exc:
            raise RuleNormalizationError("override rounding_method is invalid") from exc
        rounding_source = "override:market_overrides.yaml"
        ambiguities = ()
    elif "round down" in normalized_rules.lower():
        rounding = RoundingMethod.FLOOR
        rounding_source = "explicit:rules"
        ambiguities = ()
    elif "round up" in normalized_rules.lower():
        rounding = RoundingMethod.CEILING
        rounding_source = "explicit:rules"
        ambiguities = ()
    elif "whole degrees" in normalized_rules.lower():
        rounding = RoundingMethod.HALF_UP
        rounding_source = "inferred:whole-degree-source"
        ambiguities = ("rounding_method_inferred",)
    else:
        raise RuleNormalizationError("rounding precision is not explicit in the rules")

    buckets = _validate_complete_buckets(
        tuple(parse_bucket(market.group_item_title, unit) for market in event.markets)
    )
    return NormalizedEvent(
        event_id=event.id,
        location_name=location_match["location"],
        latitude=station.latitude,
        longitude=station.longitude,
        station_id=station.station_id,
        local_date=_parse_date(normalized_rules),
        timezone=station.timezone,
        measurement="daily_max_temperature",
        unit=unit,
        resolution_source=source,
        rounding_method=rounding,
        reporting_period="local_calendar_day",
        buckets=buckets,
        confidence_score=100 - 5 * len(ambiguities),
        field_provenance={
            "location_name": "explicit:event_title",
            "station_id": "explicit:resolution_source",
            "coordinates": "configured:stations.yaml",
            "local_date": "explicit:rules",
            "timezone": "configured:stations.yaml",
            "measurement": "explicit:rules",
            "unit": "explicit:rules",
            "rounding_method": rounding_source,
        },
        ambiguities=ambiguities,
        original_rules=event.description,
    )
