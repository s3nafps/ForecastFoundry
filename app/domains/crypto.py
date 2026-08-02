import re

from app.domains.base import DomainRoute, MarketInput, NormalizedMarket


class CryptoPlugin:
    name = "crypto"
    _assets = re.compile(r"\b(?:btc|bitcoin|eth|ethereum)\b", re.IGNORECASE)
    _comparisons = re.compile(r"\b(?:above|below|up|down|higher|lower|threshold)\b", re.IGNORECASE)
    _sources = re.compile(
        r"\b(?:coinbase|binance|kraken|chainlink|oracle|index)\b", re.IGNORECASE
    )

    def matches(self, market: MarketInput) -> bool:
        text = f"{market.title} {market.description}"
        return bool(
            self._assets.search(text)
            and (self._comparisons.search(text) or self._sources.search(text))
        )

    def normalize(self, market: MarketInput) -> DomainRoute:
        contract = NormalizedMarket(
            market_id=market.market_id,
            domain=self.name,
            resolution_source=market.description or "named source required",
            time_semantics="market-defined UTC expiry",
            unit_or_quote="crypto quote required",
            provenance={"title": "polymarket", "description": "polymarket"},
        )
        return DomainRoute(accepted=True, domain=self.name, contract=contract)
