"""Persisted BTC/ETH paper prediction path."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domains.base import MarketInput
from app.domains.crypto import CryptoContract
from app.domains.registry import DomainRegistry
from app.models import DomainContract, EvidenceSnapshot, PredictionFeature, PredictionRun
from app.services.crypto_data import CryptoMarketDataClient
from app.services.crypto_probability import Comparison, estimate_crypto_probability


class CryptoPaperPipeline:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        data: CryptoMarketDataClient,
        *,
        registry: DomainRegistry | None = None,
    ) -> None:
        self.sessions = sessions
        self.data = data
        self.registry = registry or DomainRegistry(strict_weather=True)

    async def run(
        self,
        market: MarketInput,
        *,
        now: datetime | None = None,
        seed: int = 17,
        samples: int = 2_000,
    ) -> dict[str, object]:
        captured_at = now or datetime.now(UTC)
        route = self.registry.route(market)
        if not route.accepted or not isinstance(route.contract, CryptoContract):
            return {
                "market_id": market.market_id,
                "status": "rejected",
                "reasons": list(route.reasons),
            }
        contract = route.contract
        if contract.source not in {"coinbase", "binance", "kraken"}:
            return {
                "market_id": market.market_id,
                "status": "rejected",
                "reasons": ["unsupported_public_source"],
            }
        series = await self.data.fetch_series(
            contract.source,
            asset=contract.asset,
            quote=contract.quote,
            now=captured_at,
        )
        current_price = series.candles[-1].close
        estimate = estimate_crypto_probability(
            series.log_returns,
            current_price=current_price,
            comparison=cast(Comparison, contract.comparison),
            threshold=contract.threshold,
            horizon=1,
            seed=seed,
            samples=samples,
        )
        contract_payload = contract.model_dump(mode="json")
        contract_hash = hashlib.sha256(
            json.dumps(
                {"market_id": market.market_id, "contract": contract_payload},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        evidence_payload = {
            "market_id": market.market_id,
            "source": series.source,
            "asset": contract.asset,
            "quote": contract.quote,
            "latest_price": str(current_price),
            "latest_at": series.latest_at.isoformat(),
            "candle_count": len(series.candles),
        }
        input_hash = hashlib.sha256(
            json.dumps(
                {"contract": contract_payload, "evidence": evidence_payload},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self.sessions() as session:
            domain_contract = await session.scalar(
                select(DomainContract).where(DomainContract.fingerprint == contract_hash)
            )
            if domain_contract is None:
                domain_contract = DomainContract(
                    market_external_id=market.market_id,
                    domain="crypto",
                    accepted=True,
                    resolution_source=contract.resolution_source,
                    expiry=contract.expiry,
                    contract_data=contract_payload,
                    rejection_reasons=[],
                    provenance=contract.provenance,
                    fingerprint=contract_hash,
                )
                session.add(domain_contract)
                await session.flush()
            evidence = EvidenceSnapshot(
                provider=series.source,
                provider_version="public-candles-v1",
                source_timestamp=series.latest_at,
                retrieved_at=captured_at,
                raw_response_hash=input_hash,
                normalized_values=evidence_payload,
                quality_flags=list(series.quality_flags),
                freshness_seconds=max(0, int((captured_at - series.latest_at).total_seconds())),
                license_metadata={"classification": "authoritative"},
            )
            session.add(evidence)
            prediction = PredictionRun(
                contract_id=domain_contract.id,
                generated_at=captured_at,
                model_name=estimate.model_version,
                model_version=estimate.model_version,
                input_hash=input_hash,
                parameters={"seed": seed, "samples": samples, "horizon": 1},
                probabilities={"event": str(estimate.probability)},
                uncertainty="bootstrap_monte_carlo_spread",
                status="paper_candidate",
            )
            session.add(prediction)
            await session.flush()
            session.add(
                PredictionFeature(
                    prediction_run_id=prediction.id,
                    name="latest_price",
                    value=str(current_price),
                    source=series.source,
                    live_eligible=True,
                )
            )
            await session.commit()
        return {
            "market_id": market.market_id,
            "status": "paper_candidate",
            "source": series.source,
            "probability": str(estimate.probability),
            "input_hash": input_hash,
            "evidence": evidence_payload,
            "prediction_model": estimate.model_version,
        }
