"""Source-specific production settlement evidence acquisition."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DomainContract, Observation, PaperPosition, Signal
from app.schemas import Bucket, RoundingMethod
from app.services.crypto_data import (
    CryptoDataQualityError,
    CryptoMarketDataClient,
    canonical_payload_hash,
    normalize_crypto_settlement_payload,
)
from app.services.paper import SettlementEvidence
from app.services.probability import round_temperature


class SettlementFetchError(RuntimeError):
    pass


class ProductionSettlementFetcher:
    """Fetch authoritative evidence without holding the settlement transaction."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        crypto: CryptoMarketDataClient,
    ) -> None:
        self.sessions = sessions
        self.crypto = crypto

    async def __call__(self, position_id: int) -> SettlementEvidence:
        async with self.sessions() as session:
            position = await session.get(PaperPosition, position_id)
            signal = await session.get(Signal, position.signal_id) if position else None
            contract = (
                await session.get(DomainContract, signal.contract_id)
                if signal and signal.contract_id is not None
                else None
            )
        if position is None or signal is None or contract is None or contract.expiry is None:
            raise SettlementFetchError("settlement_identity_missing")
        if contract.domain == "crypto":
            return await self._crypto(contract)
        if contract.domain == "weather":
            return await self._weather(contract, signal)
        raise SettlementFetchError(f"settlement_domain_unsupported:{contract.domain}")

    async def _crypto(self, contract: DomainContract) -> SettlementEvidence:
        data = contract.contract_data
        try:
            source = str(data["source"])
            asset = str(data["asset"])
            quote = str(data["quote"])
            assert contract.expiry is not None
            series = await self.crypto.fetch_series(
                source,
                asset=asset,
                quote=quote,
                now=datetime.now(UTC),
                granularity="1h",
                limit=200,
            )
            payload = {
                "request": {"url": series.request_url, "query": series.request_params},
                "response": series.raw_payload,
            }
            normalized = normalize_crypto_settlement_payload(
                source,
                payload,
                asset=asset,
                quote=quote,
                expiry=contract.expiry,
            )
        except (AssertionError, KeyError, ValueError, CryptoDataQualityError) as exc:
            raise SettlementFetchError(f"crypto_settlement_evidence_unavailable:{exc}") from exc
        normalized_values: dict[str, object] = dict(normalized)
        return SettlementEvidence(
            contract_id=contract.id,
            source=source,
            observed_at=contract.expiry,
            retrieved_at=series.retrieved_at,
            outcome_label=None,
            raw_response_hash=canonical_payload_hash(payload),
            raw_payload=payload,
            normalized_values=normalized_values,
            provider_version=series.provider_version,
            quality_flags=series.quality_flags,
            license_metadata={"classification": "public", "evidence_role": "settlement"},
        )

    async def _weather(self, contract: DomainContract, signal: Signal) -> SettlementEvidence:
        data = contract.contract_data
        try:
            station = str(data["station_id"])
            source = str(contract.resolution_source)
            timezone = str(data["timezone"])
            market_date = datetime.fromisoformat(str(data["local_date"])).date()
            rounding = RoundingMethod(str(data["rounding_method"]))
            raw_buckets = data["buckets"]
            if not isinstance(raw_buckets, list):
                raise TypeError("weather buckets must be a list")
            buckets = tuple(Bucket.model_validate(item) for item in raw_buckets)
        except (KeyError, TypeError, ValueError) as exc:
            raise SettlementFetchError("weather_settlement_contract_invalid") from exc
        zone = ZoneInfo(timezone)
        window_end = datetime.combine(market_date + timedelta(days=1), time.min, zone).astimezone(
            UTC
        )
        if datetime.now(UTC) < window_end:
            raise SettlementFetchError("weather_reporting_window_incomplete")
        async with self.sessions() as session:
            candidates = (
                await session.scalars(
                    select(Observation).where(
                        Observation.station_id == station,
                        Observation.source == source,
                    )
                )
            ).all()
        rows: list[tuple[Observation, float]] = []
        for row in candidates:
            if row.observed_at.astimezone(zone).date() != market_date:
                continue
            temperature = row.air_temperature
            if temperature is None:
                continue
            rows.append((row, temperature))
        if not rows:
            raise SettlementFetchError("weather_observations_missing")
        rounded = round_temperature(max(temperature for _, temperature in rows), rounding)
        bucket = next((item for item in buckets if item.contains(rounded)), None)
        if bucket is None or not signal.outcome_label:
            raise SettlementFetchError("weather_observation_outcome_unmapped")
        payload = {
            "provider": "aviation_weather",
            "query": {
                "station_id": station,
                "source": source,
                "local_date": market_date.isoformat(),
                "timezone": timezone,
            },
            "observations": [
                {
                    "icaoId": row.station_id,
                    "obsTime": int(row.observed_at.timestamp()),
                    "temp": temperature,
                    "rawOb": str(row.raw_data.get("rawOb") or "") if row.raw_data else "",
                    "receiptTime": int(row.retrieved_at.timestamp()),
                    "quality_flags": row.quality_flags,
                }
                for row, temperature in rows
            ],
        }
        assert contract.expiry is not None
        return SettlementEvidence(
            contract_id=contract.id,
            source=source,
            observed_at=contract.expiry,
            retrieved_at=datetime.now(UTC),
            outcome_label=None,
            raw_response_hash=canonical_payload_hash(payload),
            raw_payload=payload,
            normalized_values={
                "station_id": station,
                "source": source,
                "local_date": market_date.isoformat(),
                "rounded_value": str(rounded),
                "bucket_label": bucket.label,
            },
            provider_version="persisted-observations-v1",
            quality_flags=tuple(
                dict.fromkeys(
                    flag
                    for row, _ in rows
                    for flag in row.quality_flags
                    if str(flag).lower() not in {"fatal", "invalid", "missing_temperature"}
                )
            ),
            license_metadata={"evidence_role": "settlement", "source": source},
        )
