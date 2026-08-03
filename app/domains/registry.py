from app.domains.base import DomainPlugin, DomainRoute, MarketInput
from app.domains.crypto import CryptoPlugin
from app.domains.weather import WeatherPlugin


class DomainRegistry:
    def __init__(
        self,
        plugins: tuple[DomainPlugin, ...] | None = None,
        *,
        allow_legacy_unresolved_weather: bool = False,
    ) -> None:
        self._plugins = plugins or (
            WeatherPlugin(allow_legacy_unresolved=allow_legacy_unresolved_weather),
            CryptoPlugin(),
        )

    def route(self, market: MarketInput) -> DomainRoute:
        matches = tuple(plugin for plugin in self._plugins if plugin.matches(market))
        if not matches:
            return DomainRoute(accepted=False, reasons=("unsupported_domain",))
        if len(matches) > 1:
            return DomainRoute(accepted=False, reasons=("ambiguous_domain",))
        return matches[0].normalize(market)
