from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domains.base import MarketInput, NormalizedMarket
from app.services.agent_scan import AgentScanRunner, EvidenceBundle, Prediction

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


async def evidence(contract: NormalizedMarket, now: datetime) -> EvidenceBundle:
    return EvidenceBundle(
        provider=contract.resolution_source,
        captured_at=now,
        values={"value": "100"},
        quality_flags=(),
    )


async def prediction(contract: NormalizedMarket, bundle: EvidenceBundle) -> Prediction:
    return Prediction(
        probability=Decimal("0.65"),
        model="fixture-v1",
        input_hash="fixture",
    )


@pytest.mark.asyncio
async def test_agent_scan_routes_weather_and_crypto_with_shared_outputs() -> None:
    runner = AgentScanRunner(
        evidence_collectors={"weather": evidence, "crypto": evidence},
        predictors={"weather": prediction, "crypto": prediction},
    )
    result = await runner.scan(
        (
            MarketInput(market_id="weather-1", title="Temperature 25°C", description="station"),
            MarketInput(
                market_id="crypto-1",
                title="Will BTC be above $70,000 at 2026-09-01 00:00 UTC?",
                description="Coinbase BTC-USD close, rounded to nearest dollar.",
            ),
        ),
        now=NOW,
    )

    assert [item.route.domain for item in result.accepted] == ["crypto"]
    assert result.accepted[0].prediction.probability == Decimal("0.65")
    assert result.rejected[0].market_id == "weather-1"
    assert result.rejected[0].reasons[0].startswith("weather_contract_invalid:")


@pytest.mark.asyncio
async def test_agent_scan_records_ambiguous_and_stale_rejections() -> None:
    async def stale(contract: NormalizedMarket, now: datetime) -> EvidenceBundle:
        return EvidenceBundle(
            provider=contract.resolution_source,
            captured_at=now,
            values={},
            quality_flags=("stale_data",),
        )

    runner = AgentScanRunner(
        evidence_collectors={"crypto": stale},
        predictors={"crypto": prediction},
    )
    result = await runner.scan(
        (
            MarketInput(
                market_id="bad-crypto",
                title="Will BTC be above $70,000 at 2026-09-01 00:00 UTC?",
                description="BTC-USD close",
            ),
            MarketInput(
                market_id="ambiguous",
                title="Will BTC temperature reach 25°C?",
                description="Coinbase BTC-USD close",
            ),
        ),
        now=NOW,
    )

    reasons = {item.market_id: item.reasons for item in result.rejected}
    assert "rounding_definition_missing" in reasons["bad-crypto"]
    assert reasons["ambiguous"] == ("ambiguous_domain",)
