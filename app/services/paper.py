"""Atomic, idempotent paper execution and authoritative settlement."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
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
    PaperExecutionDecision,
    PaperPosition,
    PaperSettlement,
    PredictionRun,
    Signal,
)
from app.schemas import EntryQuote
from app.services.risk import RiskLimits, size_order


class PaperTradingError(ValueError):
    pass


@dataclass(frozen=True)
class SettlementEvidence:
    """Normalized evidence fetched before opening the settlement transaction."""

    contract_id: int
    source: str
    observed_at: datetime
    retrieved_at: datetime
    outcome_label: str
    raw_response_hash: str
    normalized_values: dict[str, object]
    provider_version: str | None = None
    quality_flags: tuple[str, ...] = ()
    license_metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.contract_id <= 0 or not self.source.strip() or not self.raw_response_hash.strip():
            raise PaperTradingError("settlement evidence identity is invalid")
        if self.observed_at.tzinfo is None or self.retrieved_at.tzinfo is None:
            raise PaperTradingError("settlement timestamps must be timezone-aware")
        if self.retrieved_at < self.observed_at:
            raise PaperTradingError("settlement retrieval precedes observation")
        if self.outcome_label.strip().upper() not in {"YES", "NO"}:
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
    ) -> dict[str, object]:
        """Risk-size and atomically persist one complete paper fill per signal."""
        captured_at = now or datetime.now(UTC)
        async with self.sessions() as session:
            existing = await session.scalar(
                select(PaperExecutionDecision).where(PaperExecutionDecision.signal_id == signal_id)
            )
            if existing is not None:
                return await self._execution_result(session, existing)
            signal = await session.get(Signal, signal_id)
            if signal is None:
                raise PaperTradingError("signal not found")
            required = minimum_order_size or _required_size(signal)
            requested = requested_shares or required
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
                data_stale=signal.freshness_seconds is not None
                and signal.freshness_seconds > self.settings.polymarket_poll_seconds * 2,
            )
            record = PaperExecutionDecision(
                signal_id=signal.id,
                approved=decision.approved,
                reasons=list(decision.reasons),
                requested_shares=requested,
                approved_shares=decision.shares,
                balance_before=balance,
                exposure_before=exposure,
                daily_pnl=daily_pnl,
                created_at=captured_at,
            )
            session.add(record)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                persisted = await session.scalar(
                    select(PaperExecutionDecision).where(
                        PaperExecutionDecision.signal_id == signal_id
                    )
                )
                if persisted is None:
                    raise
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
            position = await session.get(PaperPosition, position_id)
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
        self, position_id: int, evidence: SettlementEvidence
    ) -> dict[str, object]:
        """Settle once from already-fetched authoritative evidence."""
        async with self.sessions() as session:
            existing = await session.scalar(
                select(PaperSettlement).where(PaperSettlement.position_id == position_id)
            )
            if existing is not None:
                return _settlement_result(existing)
            position = await session.get(PaperPosition, position_id)
            if position is None or position.status != "open":
                raise PaperTradingError("paper position is not open")
            signal = await session.get(Signal, position.signal_id)
            if signal is None or signal.contract_id is None:
                raise PaperTradingError("paper position contract is missing")
            contract = await session.get(DomainContract, signal.contract_id)
            if contract is None or not contract.accepted:
                raise PaperTradingError("paper position contract is not accepted")
            _validate_settlement(contract, signal, evidence)
            evidence_fingerprint = _hash(
                {
                    "kind": "settlement-evidence-v1",
                    "contract": contract.fingerprint,
                    "source": evidence.source,
                    "observed_at": evidence.observed_at.isoformat(),
                    "outcome": evidence.outcome_label.upper(),
                    "raw": evidence.raw_response_hash,
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
                        "outcome_label": evidence.outcome_label.upper(),
                        "evidence_role": "authoritative_settlement",
                    },
                    quality_flags=list(evidence.quality_flags),
                    freshness_seconds=max(
                        0, int((evidence.retrieved_at - evidence.observed_at).total_seconds())
                    ),
                    license_metadata=evidence.license_metadata,
                )
                session.add(snapshot)
                await session.flush()
            outcome = evidence.outcome_label.upper()
            won = (signal.outcome_label or "").upper() == outcome
            payout = position.shares if won else Decimal("0")
            realized = payout - position.amount
            observed = Decimal("1") if won else Decimal("0")
            brier = (signal.model_probability - observed) ** 2
            settlement = PaperSettlement(
                position_id=position.id,
                evidence_snapshot_id=snapshot.id,
                outcome_label=outcome,
                settled_at=evidence.retrieved_at,
                won=won,
                payout=payout,
                realized_pnl=realized,
                brier_score=float(brier),
                resolution_data={
                    "source": evidence.source,
                    "contract_id": contract.id,
                    "evidence_fingerprint": evidence_fingerprint,
                },
            )
            session.add(settlement)
            await session.flush()
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
                results.append(await self.lifecycle.settle_position(position_id, evidence))
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


def _validate_settlement(
    contract: DomainContract, signal: Signal, evidence: SettlementEvidence
) -> None:
    if evidence.contract_id != contract.id:
        raise PaperTradingError("settlement contract mismatch")
    if (
        not contract.resolution_source
        or evidence.source.strip().lower() != str(contract.resolution_source).strip().lower()
    ):
        raise PaperTradingError("settlement source mismatch")
    if contract.expiry is None or evidence.observed_at < contract.expiry:
        raise PaperTradingError("settlement evidence precedes contract expiry")
    if (signal.outcome_label or "").upper() not in {"YES", "NO"}:
        raise PaperTradingError("signal outcome mapping is invalid")


def _settlement_result(settlement: PaperSettlement) -> dict[str, object]:
    return {
        "position_id": settlement.position_id,
        "settlement_id": settlement.id,
        "status": "settled",
        "outcome": settlement.outcome_label,
        "won": settlement.won,
        "payout": str(settlement.payout),
        "realized_pnl": str(settlement.realized_pnl),
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
