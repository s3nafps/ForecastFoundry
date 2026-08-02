from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class TemperatureUnit(StrEnum):
    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"


class RoundingMethod(StrEnum):
    HALF_UP = "half_up"
    FLOOR = "floor"
    CEILING = "ceiling"


class GammaMarket(FrozenModel):
    id: str
    question: str
    group_item_title: str
    condition_id: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    active: bool
    closed: bool
    end_date: datetime | None
    liquidity: Decimal | None
    minimum_order_size: Decimal | None
    description: str
    resolution_source: str
    raw_data: dict[str, object]


class GammaEvent(FrozenModel):
    id: str
    title: str
    description: str
    resolution_source: str
    active: bool
    closed: bool
    end_date: datetime | None
    markets: tuple[GammaMarket, ...]
    raw_data: dict[str, object]


class Station(FrozenModel):
    station_id: str
    name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str
    source: str


class Bucket(FrozenModel):
    label: str
    lower: float | None = None
    upper: float | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    def contains(self, value: float) -> bool:
        lower_ok = (
            self.lower is None
            or value > self.lower
            or (self.lower_inclusive and value == self.lower)
        )
        upper_ok = (
            self.upper is None
            or value < self.upper
            or (self.upper_inclusive and value == self.upper)
        )
        return lower_ok and upper_ok


class NormalizedEvent(FrozenModel):
    event_id: str
    location_name: str
    latitude: float
    longitude: float
    station_id: str
    local_date: date
    timezone: str
    measurement: str
    unit: TemperatureUnit
    resolution_source: str
    rounding_method: RoundingMethod
    reporting_period: str
    buckets: tuple[Bucket, ...]
    confidence_score: int = Field(ge=0, le=100)
    field_provenance: dict[str, str]
    ambiguities: tuple[str, ...]
    original_rules: str


class ForecastPoint(FrozenModel):
    timestamp: datetime
    temperature: float | None


class MemberDailyValue(FrozenModel):
    model: str
    member_id: str
    value: float | None
    bias_correction: float = 0.0
    exclusion_reason: str | None = None


class ProbabilityResult(FrozenModel):
    valid_members: int
    excluded_members: int
    outcome_probabilities: dict[str, float]
    ensemble_spread: float
    uncertainty_score: float
    model_weights: dict[str, float]
