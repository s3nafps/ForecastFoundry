"""Dedicated paper/live execution boundary; tests inject a fake adapter."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ExecutionOrder, ReconciliationEvent
from app.services.execution_control import ExecutionControl
from app.services.execution_policy import assert_startup_safe
from app.services.risk import RiskDecision


class ExchangeAdapter(Protocol):
    async def submit(self, *, client_order_id: str, price: Decimal, size: Decimal) -> str: ...

    async def status(self, provider_order_id: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    provider: str
    price: Decimal
    size: Decimal
    market_id: int | None = None
    outcome_id: int | None = None


class ExecutorError(RuntimeError):
    pass


class Executor:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        adapter: ExchangeAdapter | None = None,
        geoblock: Callable[[], Awaitable[bool]] | None = None,
        risk_recheck: Callable[[], Awaitable[RiskDecision]] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.adapter = adapter
        self.geoblock = geoblock or _allow_paper
        self.risk_recheck = risk_recheck

    async def submit(self, intent: OrderIntent, risk: RiskDecision) -> ExecutionOrder:
        existing = await self.session.scalar(
            select(ExecutionOrder).where(
                ExecutionOrder.client_order_id == intent.client_order_id
            )
        )
        if existing is not None:
            return existing
        if not risk.approved:
            raise ExecutorError(f"risk rejected order: {', '.join(risk.reasons)}")
        control = await ExecutionControl(self.session).snapshot()
        if control.paused:
            raise ExecutorError(f"execution paused: {control.reason}")
        allowed = await self.geoblock()
        if not allowed:
            raise ExecutorError("geoblock check did not allow execution")
        live = self.settings.execution_enabled
        if live:
            assert_startup_safe(self.settings)
        if live and self.adapter is None:
            raise ExecutorError("live executor adapter is not configured")
        if not live:
            order = ExecutionOrder(
                market_id=intent.market_id,
                outcome_id=intent.outcome_id,
                mode="paper",
                provider=intent.provider,
                client_order_id=intent.client_order_id,
                provider_order_id=f"paper:{intent.client_order_id}",
                side="buy",
                price=intent.price,
                size=intent.size,
                status="filled",
                live_authorized=False,
                submitted_at=datetime.now(UTC),
                metadata_json={"paper": True, "control_revision": control.revision},
            )
            self.session.add(order)
            await self.session.flush()
            return order
        assert self.adapter is not None
        # Re-check the durable state immediately before the only external call.
        latest = await ExecutionControl(self.session).snapshot()
        if latest.paused or latest.revision != control.revision:
            raise ExecutorError("execution control changed during order validation")
        if self.risk_recheck is not None:
            refreshed_risk = await self.risk_recheck()
            if not refreshed_risk.approved:
                raise ExecutorError(
                    f"risk changed before submit: {', '.join(refreshed_risk.reasons)}"
                )
        provider_order_id = await self.adapter.submit(
            client_order_id=intent.client_order_id,
            price=intent.price,
            size=intent.size,
        )
        order = ExecutionOrder(
            market_id=intent.market_id,
            outcome_id=intent.outcome_id,
            mode="live",
            provider=intent.provider,
            client_order_id=intent.client_order_id,
            provider_order_id=provider_order_id,
            side="buy",
            price=intent.price,
            size=intent.size,
            status="submitted",
            live_authorized=True,
            submitted_at=datetime.now(UTC),
            metadata_json={"control_revision": latest.revision},
        )
        self.session.add(order)
        await self.session.flush()
        return order

    async def reconcile(self, order: ExecutionOrder) -> bool:
        if order.mode == "paper":
            return True
        if self.adapter is None or not order.provider_order_id:
            raise ExecutorError("cannot reconcile without an exchange adapter")
        observed = await self.adapter.status(order.provider_order_id)
        status = str(observed.get("status", "unknown"))
        if status == "unknown":
            self.session.add(
                ReconciliationEvent(
                    execution_order_id=order.id,
                    event_type="unknown_status",
                    status="mismatch",
                    expected={"status": order.status},
                    actual=observed,
                    occurred_at=datetime.now(UTC),
                )
            )
            await ExecutionControl(self.session).set_paused(
                True,
                actor="executor",
                reason="unknown provider order status",
            )
            return False
        expected_status = order.status
        order.status = status
        self.session.add(
            ReconciliationEvent(
                execution_order_id=order.id,
                event_type="status_check",
                status="matched" if status == expected_status else "mismatch",
                expected={"status": expected_status},
                actual=observed,
                occurred_at=datetime.now(UTC),
            )
        )
        return status == order.status


async def _allow_paper() -> bool:
    return True
