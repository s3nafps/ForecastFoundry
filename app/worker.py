import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domains.base import MarketInput
from app.domains.registry import DomainRegistry
from app.domains.weather import WeatherPlugin
from app.models import (
    Event,
    ForecastMember,
    ForecastRun,
    Market,
    NormalizedRule,
    OrderBookSnapshot,
    Outcome,
    ProbabilityEstimate,
    ProviderError,
    RejectedSignal,
    Signal,
)
from app.schemas import (
    EdgeBuffers,
    ForecastResult,
    GammaEvent,
    GammaMarket,
    MemberDailyValue,
    NormalizedEvent,
    OrderBook,
    PaperAlert,
    SignalCandidate,
    SignalPolicy,
    Station,
)
from app.services.contracts import persist_domain_contract
from app.services.forecast import ForecastProvider
from app.services.observations import (
    ObservedHour,
    apply_observations_to_points,
    load_day_observations,
)
from app.services.paper import PaperLifecycle, get_paper_balance
from app.services.probability import calculate_probabilities, daily_maximum
from app.services.rules import RuleNormalizationError, normalize_temperature_event
from app.services.signals import evaluate_signal


class PolymarketSource(Protocol):
    async def discover_temperature_events(self) -> tuple[GammaEvent, ...]: ...

    async def get_order_books(self, token_ids: Sequence[str]) -> tuple[OrderBook, ...]: ...


class TelegramNotifier(Protocol):
    async def send_signal(self, alert: PaperAlert) -> bool: ...


async def _upsert_event(session: AsyncSession, source: GammaEvent) -> Event:
    event = await session.scalar(select(Event).where(Event.polymarket_id == source.id))
    if event is None:
        event = Event(
            polymarket_id=source.id,
            title=source.title,
            original_rules=source.description,
            resolution_source=source.resolution_source,
            active=source.active,
            closed=source.closed,
            end_time=source.end_date,
            raw_data=source.raw_data,
        )
        session.add(event)
    else:
        event.title = source.title
        event.original_rules = source.description
        event.resolution_source = source.resolution_source
        event.active = source.active
        event.closed = source.closed
        event.end_time = source.end_date
        event.raw_data = source.raw_data
    await session.flush()
    return event


async def _upsert_market(
    session: AsyncSession, event: Event, source: GammaMarket
) -> tuple[Market, Outcome]:
    market = await session.scalar(select(Market).where(Market.polymarket_id == source.id))
    if market is None:
        market = Market(
            event_id=event.id,
            polymarket_id=source.id,
            condition_id=source.condition_id,
            question=source.question,
            description=source.description,
            resolution_source=source.resolution_source,
            active=source.active,
            closed=source.closed,
            close_time=source.end_date,
            liquidity=source.liquidity,
            minimum_order_size=source.minimum_order_size,
            raw_data=source.raw_data,
        )
        session.add(market)
        await session.flush()
    else:
        market.active = source.active
        market.closed = source.closed
        market.close_time = source.end_date
        market.liquidity = source.liquidity
        market.minimum_order_size = source.minimum_order_size
        market.raw_data = source.raw_data

    yes_outcome: Outcome | None = None
    for label, token_id in zip(source.outcomes, source.token_ids, strict=True):
        outcome = await session.scalar(select(Outcome).where(Outcome.token_id == token_id))
        if outcome is None:
            outcome = Outcome(market_id=market.id, label=label, token_id=token_id)
            session.add(outcome)
            await session.flush()
        if label.lower() == "yes":
            yes_outcome = outcome
    if yes_outcome is None:
        raise ValueError("temperature bucket market has no YES outcome")
    return market, yes_outcome


async def _record_rules(
    session: AsyncSession,
    market: Market,
    source: GammaMarket,
    normalized: NormalizedEvent,
) -> None:
    rule = await session.scalar(select(NormalizedRule).where(NormalizedRule.market_id == market.id))
    values = {
        "location_name": normalized.location_name,
        "latitude": normalized.latitude,
        "longitude": normalized.longitude,
        "station_id": normalized.station_id,
        "local_date": normalized.local_date,
        "timezone": normalized.timezone,
        "measurement": normalized.measurement,
        "unit": normalized.unit.value,
        "resolution_source": normalized.resolution_source,
        "rounding_method": normalized.rounding_method.value,
        "reporting_period": normalized.reporting_period,
        "confidence_score": normalized.confidence_score,
        "field_provenance": normalized.field_provenance,
        "ambiguities": list(normalized.ambiguities),
        "original_rules": normalized.original_rules,
    }
    if rule is None:
        rule = NormalizedRule(market_id=market.id, **values)
        session.add(rule)
    else:
        for key, value in values.items():
            setattr(rule, key, value)
    bucket = next(item for item in normalized.buckets if item.label == source.group_item_title)
    yes = await session.scalar(
        select(Outcome).where(Outcome.market_id == market.id, Outcome.label == "Yes")
    )
    if yes:
        yes.bucket_low = bucket.lower
        yes.bucket_high = bucket.upper
        yes.low_inclusive = bucket.lower_inclusive
        yes.high_inclusive = bucket.upper_inclusive


def _forecast_horizon(now: datetime, local_date: date, timezone: str) -> int:
    target = datetime.combine(local_date, time.max, tzinfo=ZoneInfo(timezone))
    return max(0, int((target.astimezone(UTC) - now.astimezone(UTC)).total_seconds() // 3600))


def _fingerprint(
    market_id: str, outcome: str, probability: Decimal, ask: Decimal, usable_edge: Decimal
) -> str:
    value = f"{market_id}|{outcome}|{probability}|{ask}|{usable_edge}"
    return hashlib.sha256(value.encode()).hexdigest()


async def _provider_error(
    session: AsyncSession, provider: str, operation: str, error: Exception, now: datetime
) -> None:
    session.add(
        ProviderError(
            provider=provider,
            operation=operation,
            error_type=type(error).__name__,
            message=str(error),
            details={},
            retryable=True,
            occurred_at=now,
        )
    )


async def scan_once(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    polymarket: PolymarketSource,
    forecast_providers: Sequence[ForecastProvider],
    stations: Mapping[str, Station],
    overrides: Mapping[str, Mapping[str, object]],
    telegram: TelegramNotifier | None,
    now: datetime,
) -> None:
    try:
        events = await polymarket.discover_temperature_events()
    except Exception as exc:
        async with sessions() as session:
            await _provider_error(session, "polymarket", "discover", exc, now)
            await session.commit()
        return

    registry = DomainRegistry(plugins=(WeatherPlugin(stations=stations, overrides=overrides),))
    for source_event in events:
        domain_route = registry.route(
            MarketInput(
                market_id=source_event.id,
                title=source_event.title,
                description=source_event.description,
                raw_data={"event": source_event.model_dump(mode="json")},
            )
        )
        if domain_route.domain != "weather":
            continue
        async with sessions() as session:
            event = await _upsert_event(session, source_event)
            markets: dict[str, tuple[Market, Outcome, GammaMarket]] = {}
            for source_market in source_event.markets:
                market, yes = await _upsert_market(session, event, source_market)
                markets[source_market.id] = (market, yes, source_market)
            if not domain_route.accepted:
                for market, _, _ in markets.values():
                    session.add(
                        RejectedSignal(
                            market_id=market.id,
                            generated_at=now,
                            reasons=list(domain_route.reasons),
                            candidate_data={"event_id": source_event.id},
                        )
                    )
                await session.commit()
                continue
            try:
                normalized = normalize_temperature_event(
                    source_event, stations, overrides.get(source_event.id)
                )
            except RuleNormalizationError as exc:
                for market, _, _ in markets.values():
                    session.add(
                        RejectedSignal(
                            market_id=market.id,
                            generated_at=now,
                            reasons=["ambiguous_rules"],
                            candidate_data={"error": str(exc)},
                        )
                    )
                await session.commit()
                continue

            for market, _, source_market in markets.values():
                await _record_rules(session, market, source_market, normalized)

            yes_tokens = tuple(item[1].token_id for item in markets.values())
            try:
                books = await polymarket.get_order_books(yes_tokens)
            except Exception as exc:
                await _provider_error(session, "polymarket", "books", exc, now)
                books = ()
            books_by_asset = {book.asset_id: book for book in books}

            forecasts: list[ForecastResult] = []
            for provider in forecast_providers:
                try:
                    forecasts.append(
                        await provider.get_forecast(
                            normalized.latitude,
                            normalized.longitude,
                            normalized.local_date,
                            normalized.local_date,
                            normalized.timezone,
                        )
                    )
                except Exception as exc:
                    await _provider_error(session, "weather", "forecast", exc, now)

            blend_applied = False
            observations_used = 0
            observations: tuple[ObservedHour, ...] = ()
            assert normalized.measurement == "daily_max_temperature"
            within_blend = (
                source_event.end_date is not None
                and source_event.end_date - now <= timedelta(hours=settings.observation_blend_hours)
            )
            if within_blend:
                observations = await load_day_observations(
                    session,
                    station_id=normalized.station_id,
                    source=normalized.resolution_source,
                    local_date=normalized.local_date,
                    timezone=normalized.timezone,
                )
            if within_blend and len(observations) >= settings.observation_min_count:
                observations_used = len(observations)
                blend_applied = True
                forecasts = [
                    forecast.model_copy(
                        update={
                            "members": tuple(
                                member.model_copy(
                                    update={
                                        "points": apply_observations_to_points(
                                            member.points, observations, now=now
                                        )
                                    }
                                )
                                for member in forecast.members
                            )
                        }
                    )
                    for forecast in forecasts
                ]

            daily_members: list[MemberDailyValue] = []
            for forecast in forecasts:
                for member in forecast.members:
                    value = daily_maximum(member.points, normalized.local_date, normalized.timezone)
                    daily_members.append(
                        MemberDailyValue(
                            model=forecast.model,
                            member_id=member.member_id,
                            value=value,
                            exclusion_reason=None if value is not None else "missing_local_day",
                        )
                    )
            probabilities = calculate_probabilities(
                daily_members,
                normalized.buckets,
                rounding_method=normalized.rounding_method,
                unit=normalized.unit,
                model_weights={},
            )

            for market, yes, source_market in markets.values():
                contract_input = MarketInput(
                    market_id=source_market.id,
                    title=source_market.question,
                    description=source_event.description,
                    raw_data={"event": source_event.model_dump(mode="json")},
                )
                contract_route = registry.route(contract_input)
                contract = await persist_domain_contract(session, contract_input, contract_route)
                book = books_by_asset.get(yes.token_id)
                if book:
                    session.add(
                        OrderBookSnapshot(
                            market_id=market.id,
                            outcome_id=yes.id,
                            captured_at=now,
                            bids=[level.model_dump(mode="json") for level in book.bids],
                            asks=[level.model_dump(mode="json") for level in book.asks],
                            best_bid=book.best_bid,
                            best_ask=book.best_ask,
                            spread=book.spread,
                            midpoint=book.midpoint,
                            available_depth=book.available_depth,
                            minimum_order_size=book.minimum_order_size,
                            tick_size=book.tick_size,
                            raw_data=book.raw_data,
                        )
                    )
                for forecast in forecasts:
                    run = ForecastRun(
                        market_id=market.id,
                        provider=forecast.provider,
                        model=forecast.model,
                        initialization_time=forecast.initialization_time,
                        forecast_horizon_hours=_forecast_horizon(
                            now, normalized.local_date, normalized.timezone
                        ),
                        retrieved_at=forecast.retrieved_at,
                        status="complete",
                        raw_metadata=forecast.raw_metadata,
                    )
                    session.add(run)
                    await session.flush()
                    for member in forecast.members:
                        value = daily_maximum(
                            member.points, normalized.local_date, normalized.timezone
                        )
                        session.add(
                            ForecastMember(
                                forecast_run_id=run.id,
                                member_id=member.member_id,
                                points=[point.model_dump(mode="json") for point in member.points],
                                daily_value=value,
                                bias_correction=0,
                                valid=value is not None,
                                exclusion_reason=None if value is not None else "missing_local_day",
                            )
                        )
                session.add(
                    ProbabilityEstimate(
                        market_id=market.id,
                        generated_at=now,
                        valid_members=probabilities.valid_members,
                        excluded_members=probabilities.excluded_members,
                        outcome_probabilities=probabilities.outcome_probabilities,
                        ensemble_spread=probabilities.ensemble_spread,
                        uncertainty_score=probabilities.uncertainty_score,
                        model_weights=probabilities.model_weights,
                        observations_used=observations_used,
                        blend_applied=blend_applied,
                    )
                )

                probability = Decimal(
                    str(probabilities.outcome_probabilities.get(source_market.group_item_title, 0))
                )
                balance = await get_paper_balance(session, settings.paper_starting_balance)
                candidate = SignalCandidate(
                    market_id=source_market.id,
                    outcome_label=source_market.group_item_title,
                    generated_at=now,
                    market_active=source_market.active and not source_market.closed,
                    market_close_time=source_market.end_date,
                    rules_complete=True,
                    rule_confidence=normalized.confidence_score,
                    model_probability=probability,
                    best_ask=book.best_ask if book else None,
                    spread=book.spread if book else None,
                    liquidity=source_market.liquidity or Decimal("0"),
                    minimum_order_size=(book.minimum_order_size if book else None),
                    paper_balance=balance,
                    valid_members=probabilities.valid_members,
                    observations_required=within_blend,
                    observations_stale=within_blend and not blend_applied,
                    critical_quality_flags=(),
                )
                buffers = EdgeBuffers(
                    estimated_fee=settings.estimated_fee,
                    slippage=settings.slippage_buffer,
                    uncertainty=settings.uncertainty_buffer,
                    rule_risk=settings.rule_risk_buffer,
                )
                policy = SignalPolicy(
                    min_rule_confidence=settings.min_rule_confidence,
                    min_ensemble_members=settings.min_ensemble_members,
                    min_usable_edge=settings.min_usable_edge,
                    max_spread=settings.max_spread,
                    min_liquidity=settings.min_liquidity_usd,
                )
                decision = evaluate_signal(candidate, policy, buffers)
                if not decision.accepted:
                    session.add(
                        RejectedSignal(
                            market_id=market.id,
                            generated_at=now,
                            reasons=list(decision.rejection_reasons),
                            candidate_data=candidate.model_dump(mode="json"),
                        )
                    )
                    continue

                assert book and book.best_ask is not None
                assert decision.raw_edge is not None and decision.usable_edge is not None
                fingerprint = _fingerprint(
                    source_market.id,
                    source_market.group_item_title,
                    probability,
                    book.best_ask,
                    decision.usable_edge,
                )
                duplicate = await session.scalar(
                    select(Signal).where(Signal.fingerprint == fingerprint)
                )
                if duplicate:
                    session.add(
                        RejectedSignal(
                            market_id=market.id,
                            generated_at=now,
                            reasons=["duplicate_signal"],
                            candidate_data=candidate.model_dump(mode="json"),
                        )
                    )
                    continue

                model_probabilities: dict[str, float] = {}
                for forecast in forecasts:
                    model_members = tuple(
                        MemberDailyValue(
                            model=forecast.model,
                            member_id=member.member_id,
                            value=daily_maximum(
                                member.points, normalized.local_date, normalized.timezone
                            ),
                            exclusion_reason=None,
                        )
                        for member in forecast.members
                    )
                    per_model = calculate_probabilities(
                        model_members,
                        normalized.buckets,
                        rounding_method=normalized.rounding_method,
                        unit=normalized.unit,
                        model_weights={},
                    )
                    model_probabilities[forecast.model] = float(
                        per_model.outcome_probabilities.get(source_market.group_item_title, 0.0)
                    )

                signal = Signal(
                    market_id=market.id,
                    outcome_id=yes.id,
                    contract_id=contract.id,
                    outcome_label=source_market.group_item_title,
                    generated_at=now,
                    model_probability=probability,
                    executable_ask=book.best_ask,
                    raw_edge=decision.raw_edge,
                    usable_edge=decision.usable_edge,
                    buffers={
                        "estimated_fee": str(buffers.estimated_fee),
                        "slippage": str(buffers.slippage),
                        "uncertainty": str(buffers.uncertainty),
                        "rule_risk": str(buffers.rule_risk),
                    },
                    fingerprint=fingerprint,
                    freshness_seconds=0,
                    signal_data={
                        "event_id": source_event.id,
                        "candidate": {"required_size": str(book.minimum_order_size)},
                        "market": {
                            "active": source_market.active,
                            "closed": source_market.closed,
                        },
                        "model_probabilities": model_probabilities,
                    },
                )
                session.add(signal)
                await session.flush()

                model_counts = {
                    forecast.model.upper(): (
                        sum(
                            daily_maximum(member.points, normalized.local_date, normalized.timezone)
                            is not None
                            for member in forecast.members
                        ),
                        len(forecast.members),
                    )
                    for forecast in forecasts
                }
                alert = PaperAlert(
                    question=source_event.title,
                    outcome=source_market.group_item_title,
                    model_probability=probability,
                    executable_ask=book.best_ask,
                    raw_edge=decision.raw_edge,
                    usable_edge=decision.usable_edge,
                    model_member_counts=model_counts,
                    station_id=normalized.station_id,
                    observation_summary=(
                        f"{observations_used} hourly observations blended"
                        if blend_applied
                        else "no observations blended"
                    ),
                    forecast_horizon_hours=_forecast_horizon(
                        now, normalized.local_date, normalized.timezone
                    ),
                    spread=book.spread or Decimal("0"),
                    rule_confidence=normalized.confidence_score,
                    generated_at=now,
                )
                await session.commit()
                await PaperLifecycle(sessions, settings).execute_signal(
                    signal.id,
                    actor="system:weather_worker",
                    request_id=f"weather-paper:{fingerprint}",
                    now=now,
                )
                if telegram and await telegram.send_signal(alert):
                    signal.alerted_at = now
                    await session.commit()
            await session.commit()
