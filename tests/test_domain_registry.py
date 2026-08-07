from app.domains.registry import DomainRegistry, MarketInput


def test_weather_market_routes_to_weather_plugin() -> None:
    result = DomainRegistry().route(
        MarketInput(
            market_id="weather-1", title="Will London reach 25°C?", description="temperature"
        )
    )

    assert result.accepted is False
    assert result.domain == "weather"


def test_supported_crypto_market_routes_to_crypto_plugin() -> None:
    result = DomainRegistry().route(
        MarketInput(
            market_id="crypto-1",
            title="Will BTC be above $70,000 at 2026-09-01 00:00 UTC?",
            description="Coinbase BTC-USD closing price, rounded to nearest dollar.",
        )
    )

    assert result.accepted is True
    assert result.domain == "crypto"


def test_unsupported_crypto_asset_is_rejected_at_crypto_boundary() -> None:
    result = DomainRegistry().route(
        MarketInput(
            market_id="unsupported-crypto",
            title="Will SOL be above $100 at 2026-09-01 00:00 UTC?",
            description="Coinbase SOL-USD closing price, rounded to nearest dollar.",
        )
    )

    assert result.accepted is False
    assert result.domain == "crypto"
    assert result.reasons == ("unsupported_asset",)


def test_unknown_market_is_rejected_without_guessing() -> None:
    result = DomainRegistry().route(
        MarketInput(market_id="unknown-1", title="Will a new policy pass?", description="")
    )

    assert result.accepted is False
    assert result.reasons == ("unsupported_domain",)


def test_ambiguous_market_is_rejected() -> None:
    result = DomainRegistry().route(
        MarketInput(
            market_id="ambiguous-1",
            title="Will BTC temperature reach 25°C?",
            description="Coinbase BTC-USD close",
        )
    )

    assert result.accepted is False
    assert result.reasons == ("ambiguous_domain",)
