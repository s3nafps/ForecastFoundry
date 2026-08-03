"""Application use-cases shared by HTTP, CLI, MCP, scheduler, and tests."""

import os
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import COMPATIBILITY_VERSION, PRODUCT_NAME
from app.config import Settings
from app.domains.base import MarketInput
from app.domains.registry import DomainRegistry
from app.models import (
    ApplicationSetting,
    DomainContract,
    EvidenceSnapshot,
    ExecutionOrder,
    PaperPosition,
    PaperSettlement,
    PredictionFeature,
    PredictionRun,
    ReconciliationEvent,
)
from app.providers.registry import ProviderRegistry, ProviderSecretError
from app.services.contracts import persist_domain_contract
from app.services.execution_control import ControlSnapshot, ExecutionControl
from app.services.operator_auth import OperatorAuth, OperatorAuthError


class ApplicationServiceError(RuntimeError):
    pass


class ApplicationServices:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        registry: DomainRegistry | None = None,
        providers: ProviderRegistry | None = None,
        crypto_pipeline: object | None = None,
        health_monitor: object | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.registry = registry or DomainRegistry()
        self.providers = providers or ProviderRegistry.default()
        self.crypto_pipeline = crypto_pipeline
        self.health_monitor = health_monitor

    async def list_supported_domains(self) -> dict[str, object]:
        return {
            "product_name": PRODUCT_NAME,
            "compatibility_version": COMPATIBILITY_VERSION,
            "domains": ["weather", "crypto"],
            "mode": "PAPER_ONLY" if not self.settings.execution_enabled else "LIVE_GATED",
        }

    async def scan_markets(
        self, markets: Iterable[MarketInput], *, now: datetime | None = None
    ) -> dict[str, object]:
        captured_at = now or datetime.now(UTC)
        results: list[dict[str, object]] = []
        async with self.sessions() as session:
            for raw_market in markets:
                market = (
                    raw_market
                    if isinstance(raw_market, MarketInput)
                    else MarketInput.model_validate(raw_market)
                )
                route = self.registry.route(market)
                contract = route.contract
                await persist_domain_contract(session, market, route)
                results.append(
                    {
                        "market_id": market.market_id,
                        "accepted": route.accepted,
                        "domain": contract.domain if contract else None,
                        "reasons": list(route.reasons),
                        "captured_at": captured_at.isoformat(),
                    }
                )
                if route.accepted and route.domain == "crypto" and self.crypto_pipeline is not None:
                    await session.commit()
                    pipeline_result = await self.crypto_pipeline.run(market, now=captured_at)  # type: ignore[attr-defined]
                    results[-1]["pipeline"] = pipeline_result
            await session.commit()
        return {"markets": results, "mode": "PAPER_ONLY"}

    async def get_market_evidence(self, market_id: str) -> dict[str, object]:
        async with self.sessions() as session:
            candidates = (
                await session.scalars(
                    select(EvidenceSnapshot).order_by(EvidenceSnapshot.retrieved_at.desc())
                )
            ).all()
            rows = [
                row
                for row in candidates
                if row.market_id == _as_int(market_id)
                or row.normalized_values.get("market_id") == market_id
            ]
        return {
            "market_id": market_id,
            "status": "available" if rows else "no_evidence",
            "evidence": [
                {
                    "provider": row.provider,
                    "provider_version": row.provider_version,
                    "source_timestamp": row.source_timestamp,
                    "retrieved_at": row.retrieved_at,
                    "raw_response_hash": row.raw_response_hash,
                    "normalized_values": row.normalized_values,
                    "quality_flags": row.quality_flags,
                    "freshness_seconds": row.freshness_seconds,
                    "license_metadata": row.license_metadata,
                }
                for row in rows
            ],
            "secret_values": False,
        }

    async def explain_prediction(self, market_id: str) -> dict[str, object]:
        async with self.sessions() as session:
            prediction = await session.scalar(
                select(PredictionRun)
                .outerjoin(DomainContract, PredictionRun.contract_id == DomainContract.id)
                .where(
                    (PredictionRun.market_id == _as_int(market_id))
                    | (DomainContract.market_external_id == market_id)
                )
                .order_by(PredictionRun.generated_at.desc())
            )
            features = (
                (
                    await session.scalars(
                        select(PredictionFeature).where(
                            PredictionFeature.prediction_run_id == prediction.id
                        )
                    )
                ).all()
                if prediction
                else []
            )
        if prediction is None:
            return {"market_id": market_id, "status": "no_prediction", "explanation": None}
        return {
            "market_id": market_id,
            "status": prediction.status,
            "model": prediction.model_name,
            "model_version": prediction.model_version,
            "generated_at": prediction.generated_at,
            "probabilities": prediction.probabilities,
            "uncertainty": prediction.uncertainty,
            "input_hash": prediction.input_hash,
            "parameters": prediction.parameters,
            "features": [
                {"name": feature.name, "value": feature.value, "source": feature.source}
                for feature in features
            ],
            "secret_values": False,
        }

    async def run_backtest(self, dataset: str | None = None) -> dict[str, object]:
        if not dataset:
            raise ApplicationServiceError("dataset is required for a temporal backtest")
        if not _dataset_exists(dataset):
            raise ApplicationServiceError(f"dataset not found: {dataset}")
        from app.services.backtest import run_temporal_backtest

        return run_temporal_backtest(dataset)

    async def provider_health(self) -> dict[str, object]:
        if self.health_monitor is not None:
            snapshots = await self.health_monitor.all()  # type: ignore[attr-defined]
            return {"providers": snapshots, "secret_values": False}
        statuses: list[dict[str, object]] = []
        for spec in self.providers.specs():
            configured = True
            reason = "configured"
            if spec.auth != "none":
                try:
                    self.providers.resolve_secret(spec.name)
                except ProviderSecretError:
                    configured = False
                    reason = "missing_secret"
            statuses.append(
                {
                    "name": spec.name,
                    "endpoint": spec.endpoint,
                    "auth": spec.auth,
                    "classification": spec.classification,
                    "freshness_seconds": spec.freshness_seconds,
                    "configured": configured,
                    "status": "configured" if configured else "unavailable",
                    "reason": reason,
                }
            )
        return {"providers": statuses, "secret_values": False}

    async def portfolio_status(self) -> dict[str, object]:
        async with self.sessions() as session:
            control = await ExecutionControl(session).snapshot()
            balance = await session.get(ApplicationSetting, "paper_balance")
            positions = (await session.scalars(select(PaperPosition))).all()
            settlements = (await session.scalars(select(PaperSettlement))).all()
        return {
            "mode": "PAPER_ONLY" if not self.settings.execution_enabled else "LIVE_GATED",
            "paused": control.paused,
            "control": control.as_dict(),
            "live_execution": self.settings.execution_enabled,
            "wallet_custody": False,
            "paper_balance": str(
                balance.value if balance else self.settings.paper_starting_balance
            ),
            "open_positions": sum(position.status == "open" for position in positions),
            "settlements": len(settlements),
        }

    async def reconcile_orders(self) -> dict[str, object]:
        async with self.sessions() as session:
            orders = (await session.scalars(select(ExecutionOrder))).all()
            events = (await session.scalars(select(ReconciliationEvent))).all()
        return {
            "mode": "PAPER_ONLY" if not self.settings.execution_enabled else "LIVE_GATED",
            "reconciled": all(event.status == "matched" for event in events),
            "orders": [
                {
                    "id": order.id,
                    "client_order_id": order.client_order_id,
                    "provider_order_id": order.provider_order_id,
                    "status": order.status,
                    "mode": order.mode,
                }
                for order in orders
            ],
            "events": [
                {
                    "event_type": event.event_type,
                    "status": event.status,
                    "occurred_at": event.occurred_at,
                }
                for event in events
            ],
        }

    async def control_status(self) -> ControlSnapshot:
        async with self.sessions() as session:
            return await ExecutionControl(session).snapshot()

    async def run_scheduled_scan(self, scan: Callable[[], Awaitable[None]]) -> str:
        async with self.sessions() as session:
            if (await ExecutionControl(session).snapshot()).paused:
                return "paused"
        await scan()
        return "completed"

    async def set_execution(
        self,
        paused: bool,
        *,
        token: str,
        reason: str,
        actor: str,
        request_id: str | None = None,
        expected_revision: int | None = None,
        bootstrap_token: str | None = None,
    ) -> dict[str, object]:
        async with self.sessions() as session:
            auth = OperatorAuth(
                session,
                bootstrap_token=bootstrap_token or os.getenv("FORECASTFOUNDRY_OPERATOR_TOKEN"),
            )
            permission = f"execution:{'pause' if paused else 'resume'}"
            try:
                credential = await auth.authenticate(token, permission)
            except OperatorAuthError:
                await session.commit()
                raise
            snapshot = await ExecutionControl(session).set_paused(
                paused,
                actor=f"{actor}:{credential.name}",
                reason=reason,
                request_id=request_id,
                expected_revision=expected_revision,
            )
            await session.commit()
        return snapshot.as_dict()


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _dataset_exists(dataset: str) -> bool:
    return Path(dataset).is_file()
