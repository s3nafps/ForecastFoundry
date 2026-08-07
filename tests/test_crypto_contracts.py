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


def test_rejects_up_down_contract_without_explicit_comparison_baseline() -> None:
    result = parse_crypto_market(load_fixture("eth_up_down.json"))

    assert result.accepted is False
    assert "comparison_baseline_missing" in result.reasons


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


def test_rejects_negated_and_multiple_resolution_sources() -> None:
    negated = parse_crypto_market(
        MarketInput(
            market_id="negated-source",
            title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
            description=("Not Coinbase BTC-USD closing price, rounded to nearest dollar."),
        )
    )
    multiple = parse_crypto_market(
        MarketInput(
            market_id="multiple-source",
            title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
            description=("Coinbase or Kraken BTC-USD closing price, rounded to nearest dollar."),
        )
    )

    assert "resolution_source_negated" in negated.reasons
    assert "resolution_source_ambiguous" in multiple.reasons


def test_rejects_noncanonical_and_trailing_negated_resolution_sources() -> None:
    descriptions = (
        "Coinbase will not be used for the BTC-USD closing price, rounded to a dollar.",
        "Use a BTC-USD closing price other than Coinbase, rounded to a dollar.",
        "Use another BTC-USD closing price instead of Coinbase, rounded to a dollar.",
        "A rumor mentions Coinbase; BTC-USD closing price, rounded to a dollar.",
    )

    results = tuple(
        parse_crypto_market(
            MarketInput(
                market_id=f"invalid-source-{index}",
                title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
                description=description,
            )
        )
        for index, description in enumerate(descriptions)
    )

    assert all(result.accepted is False for result in results)
    assert all(
        {"resolution_source_negated", "resolution_source_missing"} & set(result.reasons)
        for result in results
    )


def test_contract_persists_exact_comparison_and_rounding_semantics() -> None:
    result = parse_crypto_market(
        MarketInput(
            market_id="inclusive-rounded",
            title="Will BTC be at or above $100 at 2026-09-01 00:00 UTC?",
            description=("Coinbase BTC-USD closing price, rounded to nearest dollar."),
        )
    )

    assert result.accepted is True
    assert result.contract is not None
    assert result.contract.comparison == "above"
    assert result.contract.comparison_inclusive is True
    assert result.contract.rounding_increment == Decimal("1")
    assert result.contract.rounding_mode == "half_up"


def test_contract_accepts_plural_cents_rounding_definition() -> None:
    result = parse_crypto_market(
        MarketInput(
            market_id="rounded-cents",
            title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
            description="Coinbase BTC-USD closing price, rounded to cents.",
        )
    )

    assert result.accepted is True
    assert result.contract is not None
    assert result.contract.rounding_increment == Decimal("0.01")


def test_rejects_non_positive_threshold_during_normalization() -> None:
    result = parse_crypto_market(
        MarketInput(
            market_id="zero-threshold",
            title="Will BTC be above $0 at 2026-09-01 00:00 UTC?",
            description="Coinbase BTC-USD closing price, rounded to cents.",
        )
    )

    assert result.accepted is False
    assert "threshold_non_positive" in result.reasons


def test_close_alias_is_persisted_as_canonical_closing_price() -> None:
    result = parse_crypto_market(
        MarketInput(
            market_id="close-alias",
            title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
            description="Use Coinbase BTC-USD close, rounded to nearest dollar.",
        )
    )

    assert result.accepted is True
    assert result.contract is not None
    assert result.contract.price_definition == "closing price"


def test_unsupported_price_definitions_reject_before_contract_creation() -> None:
    results = tuple(
        parse_crypto_market(
            MarketInput(
                market_id=f"unsupported-price-{index}",
                title="Will BTC be above $100 at 2026-09-01 00:00 UTC?",
                description=f"Use Coinbase BTC-USD {definition}, rounded to nearest dollar.",
            )
        )
        for index, definition in enumerate(("last price", "index level", "spot price"))
    )

    assert all(result.accepted is False and result.contract is None for result in results)
    assert all("price_definition_unsupported" in result.reasons for result in results)
