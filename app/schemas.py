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


class OrderLevel(FrozenModel):
    price: Decimal = Field(ge=0, le=1)
    size: Decimal = Field(gt=0)


class OrderBook(FrozenModel):
    condition_id: str
    asset_id: str
    timestamp: str
    bids: tuple[OrderLevel, ...]
    asks: tuple[OrderLevel, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    midpoint: Decimal | None
    available_depth: Decimal
    minimum_order_size: Decimal
    tick_size: Decimal
    raw_data: dict[str, object]


class ForecastMemberSeries(FrozenModel):
    member_id: str
    points: tuple[ForecastPoint, ...]


class ForecastResult(FrozenModel):
    provider: str
    model: str
    latitude: float
    longitude: float
    timezone: str
    initialization_time: datetime | None
    retrieved_at: datetime
    members: tuple[ForecastMemberSeries, ...]
    raw_metadata: dict[str, object]


class EdgeBuffers(FrozenModel):
    estimated_fee: Decimal = Field(ge=0, le=1)
    slippage: Decimal = Field(ge=0, le=1)
    uncertainty: Decimal = Field(ge=0, le=1)
    rule_risk: Decimal = Field(ge=0, le=1)


class SignalPolicy(FrozenModel):
    min_rule_confidence: int = Field(ge=0, le=100)
    min_ensemble_members: int = Field(gt=0)
    min_usable_edge: Decimal = Field(ge=0, le=1)
    max_spread: Decimal = Field(ge=0, le=1)
    min_liquidity: Decimal = Field(ge=0)


class SignalCandidate(FrozenModel):
    market_id: str
    outcome_label: str
    generated_at: datetime
    market_active: bool
    market_close_time: datetime | None
    rules_complete: bool
    rule_confidence: int
    model_probability: Decimal
    best_ask: Decimal | None
    spread: Decimal | None
    liquidity: Decimal
    minimum_order_size: Decimal | None
    paper_balance: Decimal
    valid_members: int
    observations_required: bool
    observations_stale: bool
    critical_quality_flags: tuple[str, ...]


class EdgeResult(FrozenModel):
    raw_edge: Decimal | None
    usable_edge: Decimal | None


class SignalDecision(FrozenModel):
    accepted: bool
    rejection_reasons: tuple[str, ...]
    raw_edge: Decimal | None
    usable_edge: Decimal | None


class AlertState(FrozenModel):
    outcome_label: str
    executable_ask: Decimal
    model_probability: Decimal
    usable_edge: Decimal
    sent_at: datetime


class EntryQuote(FrozenModel):
    entry_price: Decimal
    shares: Decimal
    cost: Decimal
    fees: Decimal
    total: Decimal


class PaperAlert(FrozenModel):
    question: str
    outcome: str
    model_probability: Decimal
    executable_ask: Decimal
    raw_edge: Decimal
    usable_edge: Decimal
    model_member_counts: dict[str, tuple[int, int]]
    station_id: str
    observation_summary: str
    forecast_horizon_hours: int
    spread: Decimal
    rule_confidence: int
    generated_at: datetime
