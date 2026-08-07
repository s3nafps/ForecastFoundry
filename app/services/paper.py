"""Atomic, idempotent paper execution and authoritative settlement."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import (
    ApplicationSetting,
    CalibrationMetric,
    DomainContract,
    EvidenceSnapshot,
    ExecutionFill,
    ExecutionOrder,
    Market,
    PaperExecutionDecision,
    PaperPosition,
    PaperSettlement,
    PredictionRun,
    Signal,
)
from app.schemas import Bucket, EntryQuote, RoundingMethod
from app.services.crypto_data import (
    CryptoDataQualityError,
    canonical_payload_hash,
    normalize_crypto_settlement_payload,
)
from app.services.execution_control import ExecutionControl
from app.services.probability import round_temperature
from app.services.risk import RiskLimits, size_order
from app.services.rules import canonical_bucket_label


class PaperTradingError(ValueError):
    pass


class PaperIdempotencyConflict(RuntimeError):
    code = "idempotency_conflict"

    def __init__(self, request_id: str) -> None:
        super().__init__("paper idempotency request is already bound to different arguments")
        self.request_id = request_id

    def as_dict(self) -> dict[str, object]:
        return {"type": self.code, "message": str(self), "request_id": self.request_id}


@dataclass(frozen=True)
class SettlementEvidence:
    """Normalized evidence fetched before opening the settlement transaction."""

    contract_id: int
    source: str
    observed_at: datetime
    retrieved_at: datetime
    outcome_label: str | None
    raw_response_hash: str
    normalized_values: dict[str, object]
    provider_version: str | None = None
    quality_flags: tuple[str, ...] = ()
    license_metadata: dict[str, object] = field(default_factory=dict)
    raw_payload: object | None = None

    def __post_init__(self) -> None:
        if self.contract_id <= 0 or not self.source.strip() or not self.raw_response_hash.strip():
            raise PaperTradingError("settlement evidence identity is invalid")
        if self.observed_at.tzinfo is None or self.retrieved_at.tzinfo is None:
            raise PaperTradingError("settlement timestamps must be timezone-aware")
        if self.retrieved_at < self.observed_at:
            raise PaperTradingError("settlement retrieval precedes observation")
        if self.outcome_label is not None and self.outcome_label.strip().upper() not in {
            "YES",
            "NO",
        }:
            raise PaperTradingError("settlement outcome mapping is invalid")


def estimate_entry(
    *, ask: Decimal, shares: Decimal, fee_rate: Decimal, slippage: Decimal
) -> EntryQuote:
    if ask < 0 or ask > 1 or shares <= 0 or fee_rate < 0 or slippage < 0:
        raise PaperTradingError("paper quote inputs are invalid")
    entry_price = min(Decimal("1"), ask + slippage)
    cost = entry_price * shares
    fees = cost * fee_rate
    return EntryQuote(
        entry_price=entry_price,
        shares=shares,
        cost=cost,
        fees=fees,
        total=cost + fees,
    )


async def get_paper_balance(session: AsyncSession, starting_balance: Decimal) -> Decimal:
    setting = await session.get(ApplicationSetting, "paper_balance")
    return Decimal(str(setting.value)) if setting else starting_balance


async def _locked_paper_balance(session: AsyncSession, starting_balance: Decimal) -> Decimal:
    """Serialize portfolio mutations on the durable balance row."""
    setting = await session.scalar(
        select(ApplicationSetting)
        .where(ApplicationSetting.key == "paper_balance")
        .with_for_update()
    )
    if setting is not None:
        return Decimal(str(setting.value))
    candidate = ApplicationSetting(key="paper_balance", value=str(starting_balance))
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        setting = await session.scalar(
            select(ApplicationSetting)
            .where(ApplicationSetting.key == "paper_balance")
            .with_for_update()
        )
        if setting is None:
            raise
        return Decimal(str(setting.value))
    return starting_balance


async def _begin_money_write(session: AsyncSession) -> None:
    """Acquire SQLite's process-safe writer lock before any portfolio read."""
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))


async def _set_paper_balance(
    session: AsyncSession, balance: Decimal, starting_balance: Decimal
) -> None:
    setting = await session.get(ApplicationSetting, "paper_balance")
    if setting is None:
        setting = ApplicationSetting(key="paper_balance", value=str(starting_balance))
        session.add(setting)
    setting.value = str(balance)


async def open_paper_position(
    session: AsyncSession,
    *,
    signal: Signal,
    shares: Decimal,
    minimum_order_size: Decimal,
    starting_balance: Decimal,
    fee_rate: Decimal,
    slippage: Decimal,
) -> PaperPosition:
    """Legacy weather entry helper retained for migration compatibility."""
    if shares < minimum_order_size:
        raise PaperTradingError("paper position is below the market minimum order size")
    existing = await session.scalar(
        select(PaperPosition).where(
            PaperPosition.market_id == signal.market_id,
            PaperPosition.outcome_id == signal.outcome_id,
            PaperPosition.status == "open",
        )
    )
    if existing:
        raise PaperTradingError("an overlapping paper position is already open")
    quote = estimate_entry(
        ask=signal.executable_ask,
        shares=shares,
        fee_rate=fee_rate,
        slippage=slippage,
    )
    balance = await get_paper_balance(session, starting_balance)
    if quote.total > balance:
        raise PaperTradingError("paper entry exceeds available balance")
    position = PaperPosition(
        signal_id=signal.id,
        market_id=signal.market_id,
        outcome_id=signal.outcome_id,
        entered_at=datetime.now(UTC),
        entry_price=quote.entry_price,
        amount=quote.total,
        shares=quote.shares,
        fees=quote.fees,
        status="open",
        current_mark=quote.entry_price,
        unrealized_pnl=-quote.fees,
        realized_pnl=Decimal("0"),
        signal_data=signal.signal_data,
    )
    session.add(position)
    await _set_paper_balance(session, balance - quote.total, starting_balance)
    await session.flush()
    return position


async def settle_paper_position(
    session: AsyncSession, position_id: int, *, won: bool
) -> PaperSettlement:
    """Legacy manual weather settlement; authoritative flows use ``PaperLifecycle``."""
    existing = await session.scalar(
        select(PaperSettlement).where(PaperSettlement.position_id == position_id)
    )
    if existing is not None:
        return existing
    position = await session.get(PaperPosition, position_id)
    if position is None or position.status != "open":
        raise PaperTradingError("paper position is not open")
    signal = await session.get(Signal, position.signal_id)
    if signal is None:
        raise PaperTradingError("paper position signal is missing")
    balance = await get_paper_balance(session, Decimal("5"))
    payout = position.shares if won else Decimal("0")
    realized_pnl = payout - position.amount
    observed = Decimal("1") if won else Decimal("0")
    settlement = PaperSettlement(
        position_id=position.id,
        settled_at=datetime.now(UTC),
        won=won,
        payout=payout,
        realized_pnl=realized_pnl,
        brier_score=float((signal.model_probability - observed) ** 2),
        resolution_data={"manual": True},
    )
    position.status = "settled"
    position.current_mark = observed
    position.unrealized_pnl = Decimal("0")
    position.realized_pnl = realized_pnl
    session.add(settlement)
    await _set_paper_balance(session, balance + payout, Decimal("5"))
    await session.flush()
    return settlement


class PaperLifecycle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings

    async def execute_signal(
        self,
        signal_id: int,
        *,
        requested_shares: Decimal | None = None,
        minimum_order_size: Decimal | None = None,
        now: datetime | None = None,
        actor: str = "system:paper",
        request_id: str | None = None,
    ) -> dict[str, object]:
        """Risk-size and atomically persist one complete paper fill per signal."""
        captured_at = now or datetime.now(UTC)
        if captured_at.tzinfo is None:
            raise PaperTradingError("now_must_be_timezone_aware")
        captured_at = captured_at.astimezone(UTC)
        actor = actor.strip()
        if not actor:
            raise PaperTradingError("paper actor is required")
        async with self.sessions() as session:
            await _begin_money_write(session)
            signal = await session.scalar(
                select(Signal).where(Signal.id == signal_id).with_for_update()
            )
            if signal is None:
                raise PaperTradingError("signal not found")
            required = minimum_order_size or _required_size(signal)
            requested = requested_shares or required
            normalized_request_id = request_id or f"paper-execute:{signal.fingerprint}"
            request_fingerprint = _hash(
                {
                    "operation": "execute_paper_signal",
                    "signal": signal.fingerprint,
                    "requested_shares": _decimal_key(requested),
                    "minimum_order_size": _decimal_key(required),
                    "actor": actor,
                }
            )
            existing = await session.scalar(
                select(PaperExecutionDecision).where(PaperExecutionDecision.signal_id == signal_id)
            )
            if existing is not None:
                _assert_request_matches(
                    existing.request_id,
                    existing.actor,
                    existing.request_fingerprint,
                    normalized_request_id,
                    actor,
                    request_fingerprint,
                )
                return await self._execution_result(session, existing)
            reused_request = await session.scalar(
                select(PaperExecutionDecision).where(
                    PaperExecutionDecision.request_id == normalized_request_id
                )
            )
            if reused_request is not None:
                raise PaperIdempotencyConflict(normalized_request_id)

            eligibility = await self._eligibility_reasons(session, signal, captured_at)
            if eligibility:
                balance = await get_paper_balance(session, self.settings.paper_starting_balance)
                record = PaperExecutionDecision(
                    signal_id=signal.id,
                    request_id=normalized_request_id,
                    actor=actor,
                    request_fingerprint=request_fingerprint,
                    approved=False,
                    reasons=list(eligibility),
                    requested_shares=requested,
                    approved_shares=Decimal("0"),
                    balance_before=balance,
                    exposure_before=Decimal("0"),
                    daily_pnl=Decimal("0"),
                    created_at=captured_at,
                )
                try:
                    async with session.begin_nested():
                        session.add(record)
                        await session.flush()
                except IntegrityError:
                    persisted = await _existing_execution_decision(
                        session, signal_id, normalized_request_id
                    )
                    if persisted is None:
                        raise
                    _assert_request_matches(
                        persisted.request_id,
                        persisted.actor,
                        persisted.request_fingerprint,
                        normalized_request_id,
                        actor,
                        request_fingerprint,
                    )
                    return await self._execution_result(session, persisted)
                await session.commit()
                return await self._execution_result(session, record)

            balance = await _locked_paper_balance(session, self.settings.paper_starting_balance)
            exposure = Decimal(
                str(
                    await session.scalar(
                        select(func.coalesce(func.sum(PaperPosition.amount), 0)).where(
                            PaperPosition.status == "open"
                        )
                    )
                )
            )
            unrealized = Decimal(
                str(
                    await session.scalar(
                        select(func.coalesce(func.sum(PaperPosition.unrealized_pnl), 0)).where(
                            PaperPosition.status == "open"
                        )
                    )
                )
            )
            day_start = captured_at.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = Decimal(
                str(
                    await session.scalar(
                        select(func.coalesce(func.sum(PaperSettlement.realized_pnl), 0)).where(
                            PaperSettlement.settled_at >= day_start
                        )
                    )
                )
            )
            fill_price = min(Decimal("1"), signal.executable_ask + self.settings.slippage_buffer)
            decision = size_order(
                balance=balance,
                price=fill_price * (Decimal("1") + self.settings.estimated_fee),
                requested_shares=requested,
                current_exposure=exposure,
                daily_pnl=daily_pnl,
                minimum_order_size=required,
                limits=RiskLimits(
                    self.settings.max_trade_fraction,
                    self.settings.max_exposure_fraction,
                    self.settings.max_daily_loss_fraction,
                ),
                unrealized_pnl=unrealized,
            )
            record = PaperExecutionDecision(
                signal_id=signal.id,
                request_id=normalized_request_id,
                actor=actor,
                request_fingerprint=request_fingerprint,
                approved=decision.approved,
                reasons=list(decision.reasons),
                requested_shares=requested,
                approved_shares=decision.shares,
                balance_before=balance,
                exposure_before=exposure,
                daily_pnl=daily_pnl,
                created_at=captured_at,
            )
            try:
                async with session.begin_nested():
                    session.add(record)
                    await session.flush()
            except IntegrityError:
                persisted = await _existing_execution_decision(
                    session, signal_id, normalized_request_id
                )
                if persisted is None:
                    raise
                _assert_request_matches(
                    persisted.request_id,
                    persisted.actor,
                    persisted.request_fingerprint,
                    normalized_request_id,
                    actor,
                    request_fingerprint,
                )
                return await self._execution_result(session, persisted)
            if not decision.approved:
                await session.commit()
                return await self._execution_result(session, record)

            quote = estimate_entry(
                ask=signal.executable_ask,
                shares=decision.shares,
                fee_rate=self.settings.estimated_fee,
                slippage=self.settings.slippage_buffer,
            )
            if quote.total > balance:
                raise PaperTradingError("risk-approved paper entry exceeds available balance")
            intent = _hash(
                {
                    "kind": "paper-order-v1",
                    "signal": signal.fingerprint,
                    "price": str(signal.executable_ask),
                    "fill_price": str(quote.entry_price),
                    "shares": str(quote.shares),
                    "fees": str(quote.fees),
                }
            )
            order = ExecutionOrder(
                signal_id=signal.id,
                market_id=signal.market_id,
                outcome_id=signal.outcome_id,
                mode="paper",
                provider="paper",
                client_order_id=f"paper-{intent[:40]}",
                intent_fingerprint=intent,
                provider_order_id=f"paper:{intent}",
                side="buy",
                price=signal.executable_ask,
                size=quote.shares,
                status="filled",
                live_authorized=False,
                submitted_at=captured_at,
                metadata_json={"signal_id": signal.id, "reserved_total": str(quote.total)},
            )
            session.add(order)
            await session.flush()
            fill = ExecutionFill(
                execution_order_id=order.id,
                provider_fill_id=f"paper-fill:{intent}",
                filled_at=captured_at,
                price=quote.entry_price,
                size=quote.shares,
                fee=quote.fees,
                raw_data={"simulated": True, "slippage": str(self.settings.slippage_buffer)},
            )
            session.add(fill)
            position = PaperPosition(
                signal_id=signal.id,
                execution_order_id=order.id,
                market_id=signal.market_id,
                outcome_id=signal.outcome_id,
                entered_at=captured_at,
                entry_price=quote.entry_price,
                amount=quote.total,
                shares=quote.shares,
                fees=quote.fees,
                status="open",
                current_mark=quote.entry_price,
                unrealized_pnl=-quote.fees,
                realized_pnl=Decimal("0"),
                signal_data=signal.signal_data,
            )
            session.add(position)
            await _set_paper_balance(
                session, balance - quote.total, self.settings.paper_starting_balance
            )
            await session.commit()
            return await self._execution_result(session, record)

    async def _eligibility_reasons(
        self, session: AsyncSession, signal: Signal, now: datetime
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if (await ExecutionControl(session).snapshot()).paused:
            reasons.append("execution_paused")
        contract = (
            await session.get(DomainContract, signal.contract_id)
            if signal.contract_id is not None
            else None
        )
        if contract is None:
            reasons.append("contract_missing")
        elif not contract.accepted:
            reasons.append("contract_not_accepted")
        elif contract.expiry is None or contract.expiry <= now:
            reasons.append("contract_expired")
        market_snapshot = signal.signal_data.get("market")
        if isinstance(market_snapshot, dict):
            if market_snapshot.get("active") is not True:
                reasons.append("market_inactive")
            if market_snapshot.get("closed") is not False:
                reasons.append("market_closed")
        elif signal.market_id is not None:
            market = await session.get(Market, signal.market_id)
            if market is None or not market.active:
                reasons.append("market_inactive")
            if market is None or market.closed:
                reasons.append("market_closed")
        else:
            reasons.append("market_snapshot_missing")

        if signal.generated_at > now:
            reasons.append("signal_from_future")
        else:
            signal_age = int((now - signal.generated_at).total_seconds())
            signal_freshness = signal.freshness_seconds or 0
            if signal_freshness + signal_age > self.settings.polymarket_poll_seconds * 2:
                reasons.append("signal_stale")
        prediction = (
            await session.get(PredictionRun, signal.prediction_run_id)
            if signal.prediction_run_id is not None
            else None
        )
        if prediction is not None and prediction.generated_at > now:
            reasons.append("prediction_from_future")
        evidence = (
            await session.get(EvidenceSnapshot, signal.evidence_snapshot_id)
            if signal.evidence_snapshot_id is not None
            else None
        )
        if evidence is None and (contract is None or contract.domain != "weather"):
            reasons.append("evidence_missing")
        elif evidence is not None:
            if evidence.retrieved_at > now:
                reasons.append("evidence_retrieved_from_future")
            if evidence.source_timestamp is not None and evidence.source_timestamp > now:
                reasons.append("evidence_source_from_future")
            if evidence.retrieved_at <= now and (
                evidence.source_timestamp is None or evidence.source_timestamp <= now
            ):
                raw_limit = evidence.normalized_values.get("freshness_limit_seconds", 7200)
                limit = int(raw_limit) if isinstance(raw_limit, (int, str)) else 7200
                source_age = evidence.freshness_seconds or 0
                retrieval_age = int((now - evidence.retrieved_at).total_seconds())
                if source_age + retrieval_age > limit:
                    reasons.append("evidence_stale")
        return tuple(dict.fromkeys(reasons))

    async def _execution_result(
        self, session: AsyncSession, decision: PaperExecutionDecision
    ) -> dict[str, object]:
        if not decision.approved:
            return {
                "signal_id": decision.signal_id,
                "status": "rejected",
                "reasons": list(decision.reasons),
            }
        order = await session.scalar(
            select(ExecutionOrder).where(ExecutionOrder.signal_id == decision.signal_id)
        )
        position = await session.scalar(
            select(PaperPosition).where(PaperPosition.signal_id == decision.signal_id)
        )
        if order is None or position is None:
            raise PaperTradingError("approved paper decision is incomplete")
        return {
            "signal_id": decision.signal_id,
            "status": order.status,
            "order_id": order.id,
            "position_id": position.id,
            "client_order_id": order.client_order_id,
            "price": str(order.price),
            "size": str(order.size),
        }

    async def mark_position(self, position_id: int, mark: Decimal) -> dict[str, object]:
        if mark < 0 or mark > 1:
            raise PaperTradingError("paper mark must be between zero and one")
        async with self.sessions() as session:
            position = await session.scalar(
                select(PaperPosition).where(PaperPosition.id == position_id).with_for_update()
            )
            if position is None or position.status != "open":
                raise PaperTradingError("paper position is not open")
            position.current_mark = mark
            position.unrealized_pnl = position.shares * mark - position.amount
            await session.commit()
            return {
                "position_id": position.id,
                "mark": str(mark),
                "unrealized_pnl": str(position.unrealized_pnl),
            }

    async def settle_position(
        self,
        position_id: int,
        evidence: SettlementEvidence,
        *,
        actor: str = "system:settlement",
        request_id: str | None = None,
    ) -> dict[str, object]:
        """Settle once from already-fetched authoritative evidence."""
        actor = actor.strip()
        if not actor:
            raise PaperTradingError("settlement actor is required")
        async with self.sessions() as session:
            await _begin_money_write(session)
            position = await session.scalar(
                select(PaperPosition).where(PaperPosition.id == position_id).with_for_update()
            )
            if position is None:
                raise PaperTradingError("paper position is not open")
            existing = await session.scalar(
                select(PaperSettlement).where(PaperSettlement.position_id == position_id)
            )
            normalized_request_id = request_id or f"paper-settle:{position_id}"
            request_fingerprint = _hash(
                {
                    "operation": "settle_paper_position",
                    "position_id": position_id,
                    "actor": actor,
                    "contract_id": evidence.contract_id,
                    "source": evidence.source.strip().lower(),
                    "observed_at": _utc_key(evidence.observed_at),
                    "retrieved_at": _utc_key(evidence.retrieved_at),
                    "raw_response_hash": evidence.raw_response_hash.lower(),
                    "claimed_outcome": (
                        evidence.outcome_label.strip().upper()
                        if evidence.outcome_label is not None
                        else None
                    ),
                }
            )
            if existing is not None:
                if (
                    existing.request_id is None
                    and existing.actor is None
                    and existing.request_fingerprint is None
                ):
                    return _settlement_result(existing)
                _assert_request_matches(
                    existing.request_id,
                    existing.actor,
                    existing.request_fingerprint,
                    normalized_request_id,
                    actor,
                    request_fingerprint,
                )
                return _settlement_result(existing)
            reused_request = await session.scalar(
                select(PaperSettlement).where(PaperSettlement.request_id == normalized_request_id)
            )
            if reused_request is not None:
                raise PaperIdempotencyConflict(normalized_request_id)
            if position.status != "open":
                raise PaperTradingError("paper position is not open")
            signal = await session.get(Signal, position.signal_id)
            if signal is None or signal.contract_id is None:
                raise PaperTradingError("paper position contract is missing")
            contract = await session.get(DomainContract, signal.contract_id)
            if contract is None or not contract.accepted:
                raise PaperTradingError("paper position contract is not accepted")
            outcome = _derive_settlement_outcome(contract, signal, evidence)
            evidence_fingerprint = _hash(
                {
                    "kind": "settlement-evidence-v1",
                    "contract": contract.fingerprint,
                    "source": evidence.source.strip().lower(),
                    "observed_at": _utc_key(evidence.observed_at),
                    "raw": evidence.raw_response_hash.lower(),
                }
            )
            snapshot = await session.scalar(
                select(EvidenceSnapshot).where(EvidenceSnapshot.fingerprint == evidence_fingerprint)
            )
            if snapshot is None:
                snapshot = EvidenceSnapshot(
                    contract_id=contract.id,
                    fingerprint=evidence_fingerprint,
                    provider=evidence.source,
                    provider_version=evidence.provider_version,
                    source_timestamp=evidence.observed_at,
                    retrieved_at=evidence.retrieved_at,
                    raw_response_hash=evidence.raw_response_hash,
                    normalized_values={
                        **evidence.normalized_values,
                        "outcome_label": outcome,
                        "evidence_role": "authoritative_settlement",
                    },
                    quality_flags=list(evidence.quality_flags),
                    freshness_seconds=max(
                        0, int((evidence.retrieved_at - evidence.observed_at).total_seconds())
                    ),
                    license_metadata=evidence.license_metadata,
                )
                try:
                    async with session.begin_nested():
                        session.add(snapshot)
                        await session.flush()
                except IntegrityError:
                    snapshot = await session.scalar(
                        select(EvidenceSnapshot).where(
                            EvidenceSnapshot.fingerprint == evidence_fingerprint
                        )
                    )
                    if snapshot is None:
                        raise
            # Weather signals carry the bucketed outcome label (e.g. "24°C or
            # higher"), never YES/NO, so the derived outcome already encodes
            # whether the signal's bucket matched the resolved bucket: a weather
            # position wins iff the derived outcome is YES. Crypto signals are
            # labeled YES/NO and win when their label matches the outcome.
            if contract.domain == "weather":
                won = outcome == "YES"
            else:
                won = (signal.outcome_label or "").upper() == outcome
            payout = position.shares if won else Decimal("0")
            realized = payout - position.amount
            observed = Decimal("1") if won else Decimal("0")
            brier = (signal.model_probability - observed) ** 2
            resolution_data: dict[str, object] = {
                "source": evidence.source,
                "contract_id": contract.id,
                "evidence_fingerprint": evidence_fingerprint,
            }
            if contract.domain == "weather":
                resolution_data["resolved_bucket"] = str(
                    evidence.normalized_values.get("bucket_label", "")
                )
            settlement = PaperSettlement(
                position_id=position.id,
                evidence_snapshot_id=snapshot.id,
                request_id=normalized_request_id,
                actor=actor,
                request_fingerprint=request_fingerprint,
                outcome_label=outcome,
                settled_at=evidence.retrieved_at,
                won=won,
                payout=payout,
                realized_pnl=realized,
                brier_score=float(brier),
                resolution_data=resolution_data,
            )
            try:
                async with session.begin_nested():
                    session.add(settlement)
                    await session.flush()
            except IntegrityError:
                replay = await session.scalar(
                    select(PaperSettlement).where(
                        (PaperSettlement.position_id == position_id)
                        | (PaperSettlement.request_id == normalized_request_id)
                    )
                )
                if replay is None:
                    raise
                _assert_request_matches(
                    replay.request_id,
                    replay.actor,
                    replay.request_fingerprint,
                    normalized_request_id,
                    actor,
                    request_fingerprint,
                )
                return _settlement_result(replay)
            position.status = "settled"
            position.current_mark = Decimal("1") if won else Decimal("0")
            position.unrealized_pnl = Decimal("0")
            position.realized_pnl = realized
            balance = await _locked_paper_balance(session, self.settings.paper_starting_balance)
            await _set_paper_balance(
                session, balance + payout, self.settings.paper_starting_balance
            )
            prediction = (
                await session.get(PredictionRun, signal.prediction_run_id)
                if signal.prediction_run_id is not None
                else None
            )
            if prediction is not None:
                session.add(
                    CalibrationMetric(
                        prediction_run_id=prediction.id,
                        settlement_id=settlement.id,
                        model_name=prediction.model_name,
                        window_start=prediction.generated_at,
                        window_end=evidence.observed_at,
                        brier_score=brier,
                        reliability_buckets=[
                            {
                                "probability": str(signal.model_probability),
                                "observed": str(observed),
                                "count": 1,
                            }
                        ],
                    )
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await session.scalar(
                    select(PaperSettlement).where(PaperSettlement.position_id == position_id)
                )
                if replay is None:
                    raise
                _assert_request_matches(
                    replay.request_id,
                    replay.actor,
                    replay.request_fingerprint,
                    normalized_request_id,
                    actor,
                    request_fingerprint,
                )
                return _settlement_result(replay)
            return _settlement_result(settlement)


SettlementFetcher = Callable[[int], Awaitable[SettlementEvidence]]


class SettlementWorker:
    """Fetch outside transactions, then settle each due position independently."""

    def __init__(self, lifecycle: PaperLifecycle) -> None:
        self.lifecycle = lifecycle

    async def run_due(
        self, fetch: SettlementFetcher, *, now: datetime | None = None
    ) -> list[dict[str, object]]:
        captured_at = now or datetime.now(UTC)
        async with self.lifecycle.sessions() as session:
            due = (
                (
                    await session.execute(
                        select(PaperPosition.id)
                        .join(Signal, Signal.id == PaperPosition.signal_id)
                        .join(DomainContract, DomainContract.id == Signal.contract_id)
                        .where(
                            PaperPosition.status == "open",
                            DomainContract.expiry.is_not(None),
                            DomainContract.expiry <= captured_at,
                        )
                        .order_by(PaperPosition.id)
                    )
                )
                .scalars()
                .all()
            )
        results: list[dict[str, object]] = []
        for position_id in due:
            try:
                evidence = await fetch(position_id)
                results.append(
                    await self.lifecycle.settle_position(
                        position_id,
                        evidence,
                        actor="system:settlement_scheduler",
                        request_id=f"scheduled-settlement:{position_id}",
                    )
                )
            except Exception as exc:  # each position is an independent polling unit
                results.append(
                    {
                        "position_id": position_id,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        return results


def _required_size(signal: Signal) -> Decimal:
    candidate = signal.signal_data.get("candidate")
    value = candidate.get("required_size") if isinstance(candidate, dict) else None
    try:
        required = Decimal(str(value))
    except Exception as exc:
        raise PaperTradingError("signal minimum order size is missing") from exc
    if required <= 0:
        raise PaperTradingError("signal minimum order size is invalid")
    return required


def _derive_settlement_outcome(
    contract: DomainContract, signal: Signal, evidence: SettlementEvidence
) -> str:
    if evidence.contract_id != contract.id:
        raise PaperTradingError("settlement contract mismatch")
    if (
        not contract.resolution_source
        or evidence.source.strip().lower() != str(contract.resolution_source).strip().lower()
    ):
        raise PaperTradingError("settlement source mismatch")
    if contract.expiry is None or evidence.observed_at != contract.expiry:
        raise PaperTradingError("settlement observation timestamp mismatch")
    if contract.domain == "weather":
        return _derive_weather_settlement_outcome(contract, signal, evidence)
    if contract.domain != "crypto":
        raise PaperTradingError(f"settlement domain unsupported: {contract.domain}")
    if (signal.outcome_label or "").upper() not in {"YES", "NO"}:
        raise PaperTradingError("signal outcome mapping is invalid")
    data = contract.contract_data
    values = evidence.normalized_values
    if str(data.get("price_definition", "")).strip().lower() != "closing price":
        raise PaperTradingError("settlement price definition unsupported")
    if evidence.raw_payload is None:
        raise PaperTradingError("settlement raw payload is required")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence.raw_response_hash):
        raise PaperTradingError("settlement raw payload hash is invalid")
    if canonical_payload_hash(evidence.raw_payload) != evidence.raw_response_hash:
        raise PaperTradingError("settlement raw payload hash mismatch")
    try:
        derived = normalize_crypto_settlement_payload(
            evidence.source.strip().lower(),
            evidence.raw_payload,
            asset=str(data["asset"]),
            quote=str(data["quote"]),
            expiry=evidence.observed_at,
        )
    except (CryptoDataQualityError, KeyError, ValueError) as exc:
        raise PaperTradingError(str(exc)) from exc
    if str(values.get("asset", "")).upper() != derived["asset"]:
        raise PaperTradingError("settlement normalized asset contradicts raw payload")
    if str(values.get("quote", "")).upper() != derived["quote"]:
        raise PaperTradingError("settlement normalized quote contradicts raw payload")
    try:
        normalized_price = Decimal(str(values["price"]))
    except (KeyError, InvalidOperation) as exc:
        raise PaperTradingError("settlement normalized price contradicts raw payload") from exc
    if normalized_price != Decimal(derived["price"]):
        raise PaperTradingError("settlement normalized price contradicts raw payload")
    if str(values.get("price_definition", "")).strip().lower() != derived["price_definition"]:
        raise PaperTradingError("settlement normalized price_definition contradicts raw payload")
    try:
        normalized_timestamp = datetime.fromisoformat(
            str(values["source_timestamp"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError) as exc:
        raise PaperTradingError(
            "settlement normalized source_timestamp contradicts raw payload"
        ) from exc
    if (
        normalized_timestamp.tzinfo is None
        or _utc_key(normalized_timestamp) != derived["source_timestamp"]
    ):
        raise PaperTradingError("settlement normalized source_timestamp contradicts raw payload")
    try:
        price = Decimal(derived["price"])
        increment = Decimal(str(data["rounding_increment"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise PaperTradingError("settlement price is invalid") from exc
    if not price.is_finite() or price <= 0 or increment <= 0:
        raise PaperTradingError("settlement price is invalid")
    if data.get("rounding_mode") != "half_up":
        raise PaperTradingError("settlement rounding mode unsupported")
    rounded = (price / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment
    comparison = data.get("comparison")
    inclusive = data.get("comparison_inclusive") is True
    reference = data.get("threshold")
    if comparison in {"up", "down"}:
        reference = data.get("comparison_reference_price")
    try:
        boundary = Decimal(str(reference))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaperTradingError("settlement comparison reference is invalid") from exc
    if comparison in {"above", "up"}:
        resolved_yes = rounded >= boundary if inclusive else rounded > boundary
    elif comparison in {"below", "down"}:
        resolved_yes = rounded <= boundary if inclusive else rounded < boundary
    else:
        raise PaperTradingError("settlement comparison unsupported")
    outcome = "YES" if resolved_yes else "NO"
    if evidence.outcome_label is not None and evidence.outcome_label.strip().upper() != outcome:
        raise PaperTradingError("settlement claimed outcome conflicts with observation")
    return outcome


def _derive_weather_settlement_outcome(
    contract: DomainContract, signal: Signal, evidence: SettlementEvidence
) -> str:
    data = contract.contract_data
    if evidence.raw_payload is None:
        raise PaperTradingError("settlement raw payload is required")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence.raw_response_hash):
        raise PaperTradingError("settlement raw payload hash is invalid")
    if canonical_payload_hash(evidence.raw_payload) != evidence.raw_response_hash:
        raise PaperTradingError("settlement raw payload hash mismatch")
    if not isinstance(evidence.raw_payload, dict):
        raise PaperTradingError("weather settlement payload is invalid")
    if evidence.raw_payload.get("provider") != "aviation_weather":
        raise PaperTradingError("weather settlement provider unsupported")
    rows = evidence.raw_payload.get("observations")
    if not isinstance(rows, list) or not rows:
        raise PaperTradingError("weather settlement observations missing")
    station = str(data.get("station_id", ""))
    source = str(contract.resolution_source or "")
    timezone = str(data.get("timezone", ""))
    local_date = str(data.get("local_date", ""))
    if str(data.get("measurement", "")) != "daily_max_temperature":
        raise PaperTradingError("weather settlement measurement unsupported")
    if str(data.get("reporting_period", "")) != "local_calendar_day":
        raise PaperTradingError("weather settlement reporting period unsupported")
    unit = str(data.get("unit_or_quote", data.get("unit", ""))).lower()
    if unit not in {"celsius", "fahrenheit"}:
        raise PaperTradingError("weather settlement unit unsupported")
    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise PaperTradingError("weather settlement observation invalid")
        try:
            observed = datetime.fromtimestamp(float(row["obsTime"]), UTC)
            temperature_celsius = float(row["temp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperTradingError("weather settlement observation invalid") from exc
        if row.get("icaoId") != station:
            raise PaperTradingError("weather settlement station or source mismatch")
        flags = row.get("quality_flags", [])
        if not isinstance(flags, list) or any(
            str(flag).lower() in {"fatal", "invalid", "missing_temperature"} for flag in flags
        ):
            raise PaperTradingError("weather settlement observation quality is fatal")
        if observed.astimezone(ZoneInfo(timezone)).date().isoformat() != local_date:
            raise PaperTradingError("weather observation outside reporting window")
        temperature = (
            temperature_celsius * 9 / 5 + 32 if unit == "fahrenheit" else temperature_celsius
        )
        parsed.append((observed, temperature))
    parsed.sort(key=lambda item: item[0])
    timestamps = [item[0] for item in parsed]
    if len(set(timestamps)) != len(timestamps):
        raise PaperTradingError("weather settlement observations contain duplicates")
    zone = ZoneInfo(timezone)
    local_day = datetime.fromisoformat(local_date).date()
    window_start = datetime.combine(local_day, datetime.min.time(), zone).astimezone(UTC)
    window_end = datetime.combine(
        local_day + timedelta(days=1), datetime.min.time(), zone
    ).astimezone(UTC)
    if (
        len(parsed) < 6
        or timestamps[0] - window_start > timedelta(hours=3)
        or window_end - timestamps[-1] > timedelta(hours=3)
        or any(
            current - previous > timedelta(hours=4)
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        )
    ):
        raise PaperTradingError("weather settlement observation coverage inadequate")
    try:
        rounded = round_temperature(
            max(value for _, value in parsed), RoundingMethod(str(data["rounding_method"]))
        )
        raw_buckets = data["buckets"]
        if not isinstance(raw_buckets, list):
            raise TypeError("weather buckets must be a list")
        buckets = tuple(Bucket.model_validate(item) for item in raw_buckets)
    except (KeyError, TypeError, ValueError) as exc:
        raise PaperTradingError("weather settlement contract invalid") from exc
    bucket = next((item for item in buckets if item.contains(rounded)), None)
    if bucket is None:
        raise PaperTradingError("weather observation maps to no outcome bucket")
    expected = signal.outcome_label
    if not expected:
        raise PaperTradingError("weather signal outcome label missing")
    outcome = (
        "YES"
        if canonical_bucket_label(str(expected)) == canonical_bucket_label(bucket.label)
        else "NO"
    )
    normalized = evidence.normalized_values
    if normalized.get("station_id") != station:
        raise PaperTradingError("weather normalized station contradicts observations")
    if normalized.get("source") != source:
        raise PaperTradingError("weather normalized source contradicts observations")
    if normalized.get("local_date") != local_date:
        raise PaperTradingError("weather normalized date contradicts observations")
    if Decimal(str(normalized.get("rounded_value"))) != Decimal(str(rounded)):
        raise PaperTradingError("weather normalized value contradicts observations")
    if canonical_bucket_label(str(normalized.get("bucket_label"))) != canonical_bucket_label(
        bucket.label
    ):
        raise PaperTradingError("weather normalized bucket contradicts observations")
    if evidence.outcome_label is not None and evidence.outcome_label.strip().upper() != outcome:
        raise PaperTradingError("settlement claimed outcome conflicts with observation")
    return outcome


def _assert_request_matches(
    stored_request_id: str | None,
    stored_actor: str | None,
    stored_fingerprint: str | None,
    request_id: str,
    actor: str,
    fingerprint: str,
) -> None:
    if (
        stored_request_id != request_id
        or stored_actor != actor
        or stored_fingerprint != fingerprint
    ):
        raise PaperIdempotencyConflict(request_id)


async def _existing_execution_decision(
    session: AsyncSession, signal_id: int, request_id: str
) -> PaperExecutionDecision | None:
    """Recover the winner after a signal-id or request-id uniqueness race."""
    return cast(
        PaperExecutionDecision | None,
        await session.scalar(
            select(PaperExecutionDecision).where(
                (PaperExecutionDecision.signal_id == signal_id)
                | (PaperExecutionDecision.request_id == request_id)
            )
        ),
    )


def _settlement_result(settlement: PaperSettlement) -> dict[str, object]:
    return {
        "position_id": settlement.position_id,
        "settlement_id": settlement.id,
        "status": "settled",
        "outcome": settlement.outcome_label,
        "won": settlement.won,
        "payout": _money(settlement.payout),
        "realized_pnl": _money(settlement.realized_pnl),
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def _decimal_key(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _utc_key(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
