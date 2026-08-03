from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domains.base import DomainRoute, MarketInput, NormalizedMarket
from app.domains.registry import DomainRegistry


@dataclass(frozen=True)
class EvidenceBundle:
    provider: str
    captured_at: datetime
    values: Mapping[str, object]
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prediction:
    probability: Decimal
    model: str
    input_hash: str


@dataclass(frozen=True)
class AcceptedScan:
    market_id: str
    route: DomainRoute
    evidence: EvidenceBundle
    prediction: Prediction


@dataclass(frozen=True)
class RejectedScan:
    market_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AgentScanResult:
    accepted: tuple[AcceptedScan, ...]
    rejected: tuple[RejectedScan, ...]


EvidenceCollector = Callable[[NormalizedMarket, datetime], Awaitable[EvidenceBundle]]
Predictor = Callable[[NormalizedMarket, EvidenceBundle], Awaitable[Prediction]]


class AgentScanRunner:
    def __init__(
        self,
        *,
        registry: DomainRegistry | None = None,
        evidence_collectors: Mapping[str, EvidenceCollector],
        predictors: Mapping[str, Predictor],
    ) -> None:
        self._registry = registry or DomainRegistry()
        self._evidence_collectors = evidence_collectors
        self._predictors = predictors

    async def scan(self, markets: Iterable[MarketInput], *, now: datetime) -> AgentScanResult:
        accepted: list[AcceptedScan] = []
        rejected: list[RejectedScan] = []
        for market in markets:
            route = self._registry.route(market)
            if not route.accepted or route.contract is None or route.domain is None:
                rejected.append(RejectedScan(market.market_id, route.reasons))
                continue
            collector = self._evidence_collectors.get(route.domain)
            predictor = self._predictors.get(route.domain)
            if collector is None:
                rejected.append(RejectedScan(market.market_id, ("evidence_provider_missing",)))
                continue
            if predictor is None:
                rejected.append(RejectedScan(market.market_id, ("predictor_missing",)))
                continue
            try:
                evidence = await collector(route.contract, now)
            except Exception as exc:
                rejected.append(
                    RejectedScan(market.market_id, (f"evidence_error:{type(exc).__name__}",))
                )
                continue
            if evidence.quality_flags:
                rejected.append(RejectedScan(market.market_id, evidence.quality_flags))
                continue
            try:
                prediction = await predictor(route.contract, evidence)
            except Exception as exc:
                rejected.append(
                    RejectedScan(market.market_id, (f"prediction_error:{type(exc).__name__}",))
                )
                continue
            if not 0 <= prediction.probability <= 1:
                rejected.append(RejectedScan(market.market_id, ("probability_out_of_bounds",)))
                continue
            accepted.append(AcceptedScan(market.market_id, route, evidence, prediction))
        return AgentScanResult(tuple(accepted), tuple(rejected))
