from app.domains.base import DomainRoute, MarketInput, NormalizedMarket


class WeatherPlugin:
    name = "weather"

    def matches(self, market: MarketInput) -> bool:
        text = f"{market.title} {market.description}".lower()
        return any(
            term in text for term in ("temperature", "°c", "°f", "rainfall", "precipitation")
        )

    def normalize(self, market: MarketInput) -> DomainRoute:
        contract = NormalizedMarket(
            market_id=market.market_id,
            domain=self.name,
            resolution_source=market.description or "Polymarket rules",
            time_semantics="market-defined observation window",
            unit_or_quote="weather measurement",
            provenance={"title": "polymarket", "description": "polymarket"},
        )
        return DomainRoute(accepted=True, domain=self.name, contract=contract)
