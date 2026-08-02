from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTC timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    polymarket_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    original_rules: Mapped[str] = mapped_column(Text)
    resolution_source: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, index=True)
    closed: Mapped[bool] = mapped_column(Boolean, index=True)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    raw_data: Mapped[dict[str, object]] = mapped_column(JSON)


class Market(TimestampMixin, Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    polymarket_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    condition_id: Mapped[str] = mapped_column(String(80), unique=True)
    question: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    resolution_source: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, index=True)
    closed: Mapped[bool] = mapped_column(Boolean, index=True)
    close_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    liquidity: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    minimum_order_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    raw_data: Mapped[dict[str, object]] = mapped_column(JSON)


class Outcome(TimestampMixin, Base):
    __tablename__ = "outcomes"
    __table_args__ = (UniqueConstraint("market_id", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    token_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    bucket_low: Mapped[float | None] = mapped_column(Float)
    bucket_high: Mapped[float | None] = mapped_column(Float)
    low_inclusive: Mapped[bool] = mapped_column(Boolean, default=True)
    high_inclusive: Mapped[bool] = mapped_column(Boolean, default=True)


class OrderBookSnapshot(Base):
    __tablename__ = "order_book_snapshots"
    __table_args__ = (Index("ix_order_books_market_captured", "market_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime())
    bids: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    asks: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    midpoint: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    available_depth: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    minimum_order_size: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    tick_size: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    raw_data: Mapped[dict[str, object]] = mapped_column(JSON)


class NormalizedRule(TimestampMixin, Base):
    __tablename__ = "normalized_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), unique=True
    )
    location_name: Mapped[str] = mapped_column(String(160))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    station_id: Mapped[str] = mapped_column(String(32), index=True)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    timezone: Mapped[str] = mapped_column(String(80))
    measurement: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(24))
    resolution_source: Mapped[str] = mapped_column(Text)
    rounding_method: Mapped[str] = mapped_column(String(32))
    reporting_period: Mapped[str] = mapped_column(String(80))
    confidence_score: Mapped[int] = mapped_column(Integer)
    field_provenance: Mapped[dict[str, str]] = mapped_column(JSON)
    ambiguities: Mapped[list[str]] = mapped_column(JSON)
    original_rules: Mapped[str] = mapped_column(Text)


class ForecastRun(Base):
    __tablename__ = "forecast_runs"
    __table_args__ = (Index("ix_forecast_runs_provider_retrieved", "provider", "retrieved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(80))
    initialization_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    forecast_horizon_hours: Mapped[int | None] = mapped_column(Integer)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(32))
    raw_metadata: Mapped[dict[str, object]] = mapped_column(JSON)


class ForecastMember(Base):
    __tablename__ = "forecast_members"
    __table_args__ = (UniqueConstraint("forecast_run_id", "member_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    forecast_run_id: Mapped[int] = mapped_column(
        ForeignKey("forecast_runs.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[str] = mapped_column(String(80))
    points: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    daily_value: Mapped[float | None] = mapped_column(Float)
    bias_correction: Mapped[float] = mapped_column(Float, default=0.0)
    valid: Mapped[bool] = mapped_column(Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(String(160))


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (Index("ix_observations_station_time", "station_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id", ondelete="SET NULL"))
    station_id: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    air_temperature: Mapped[float | None] = mapped_column(Float)
    precipitation: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80))
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime())
    quality_flags: Mapped[list[str]] = mapped_column(JSON)
    raw_data: Mapped[dict[str, object]] = mapped_column(JSON)


class ProbabilityEstimate(Base):
    __tablename__ = "probability_estimates"
    __table_args__ = (Index("ix_probabilities_market_generated", "market_id", "generated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    valid_members: Mapped[int] = mapped_column(Integer)
    excluded_members: Mapped[int] = mapped_column(Integer)
    outcome_probabilities: Mapped[dict[str, float]] = mapped_column(JSON)
    ensemble_spread: Mapped[float] = mapped_column(Float)
    uncertainty_score: Mapped[float] = mapped_column(Float)
    model_weights: Mapped[dict[str, float]] = mapped_column(JSON)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_market_generated", "market_id", "generated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id", ondelete="CASCADE"))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    model_probability: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    executable_ask: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    raw_edge: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    usable_edge: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    buffers: Mapped[dict[str, str]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    alert_error: Mapped[str | None] = mapped_column(Text)
    signal_data: Mapped[dict[str, object]] = mapped_column(JSON)


class RejectedSignal(Base):
    __tablename__ = "rejected_signals"
    __table_args__ = (Index("ix_rejections_market_generated", "market_id", "generated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id", ondelete="SET NULL"))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    reasons: Mapped[list[str]] = mapped_column(JSON)
    candidate_data: Mapped[dict[str, object]] = mapped_column(JSON)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        Index("ix_paper_positions_status", "status"),
        UniqueConstraint("market_id", "outcome_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), unique=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id"))
    entered_at: Mapped[datetime] = mapped_column(UTCDateTime())
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(String(24), default="open")
    signal_data: Mapped[dict[str, object]] = mapped_column(JSON)


class PaperSettlement(Base):
    __tablename__ = "paper_settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"), unique=True)
    settled_at: Mapped[datetime] = mapped_column(UTCDateTime())
    won: Mapped[bool] = mapped_column(Boolean)
    payout: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    brier_score: Mapped[float] = mapped_column(Float)
    resolution_data: Mapped[dict[str, object]] = mapped_column(JSON)


class ProviderError(Base):
    __tablename__ = "provider_errors"
    __table_args__ = (Index("ix_provider_errors_provider_time", "provider", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(80))
    operation: Mapped[str] = mapped_column(String(120))
    error_type: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, object]] = mapped_column(JSON)
    retryable: Mapped[bool] = mapped_column(Boolean)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ApplicationSetting(Base):
    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
