import json
from decimal import Decimal
from pathlib import Path

from app.domains.base import MarketInput
from app.domains.crypto import parse_crypto_market

FIXTURES = Path(__file__).parent / "fixtures" / "markets" / "crypto"


def load_fixture(name: str) -> MarketInput:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return MarketInput.model_validate(payload)


def test_parses_canonical_btc_threshold_contract() -> None:
    result = parse_crypto_market(load_fixture("btc_threshold.json"))

    assert result.accepted is True
    assert result.contract is not None
    assert result.contract.asset == "BTC"
    assert result.contract.quote == "USD"
    assert result.contract.source == "coinbase"
    assert result.contract.threshold == Decimal("70000")
    assert result.contract.expiry.isoformat() == "2026-09-01T00:00:00+00:00"


def test_parses_eth_up_down_contract() -> None:
    result = parse_crypto_market(load_fixture("eth_up_down.json"))

    assert result.accepted is True
    assert result.contract is not None
    assert result.contract.asset == "ETH"
    assert result.contract.comparison == "up"
    assert result.contract.threshold is None


def test_rejects_missing_source_and_rounding() -> None:
    result = parse_crypto_market(
        MarketInput(
            market_id="bad-1",
            title="Will BTC be above $70,000 at 2026-09-01 00:00 UTC?",
            description="BTC-USD closing price",
        )
    )

    assert result.accepted is False
    assert "resolution_source_missing" in result.reasons
    assert "rounding_definition_missing" in result.reasons


def test_rejects_ambiguous_generic_crypto_text() -> None:
    result = parse_crypto_market(
        MarketInput(
            market_id="bad-2",
            title="Will Bitcoin go up?",
            description="",
        )
    )

    assert result.accepted is False
    assert "quote_missing" in result.reasons
    assert "resolution_source_missing" in result.reasons
    assert "expiry_missing" in result.reasons
