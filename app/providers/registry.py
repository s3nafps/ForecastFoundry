import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class ProviderSecretError(ValueError):
    """Raised when a keyed provider has no configured secret."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    endpoint: str
    auth: str
    secret_env: str | None
    quota: str
    attribution: str
    freshness_seconds: int
    classification: str


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    healthy: bool
    reason: str


class ProviderRegistry:
    def __init__(self, specs: tuple[ProviderSpec, ...]) -> None:
        self._specs = {spec.name: spec for spec in specs}

    @classmethod
    def default(cls) -> "ProviderRegistry":
        return cls(
            (
                ProviderSpec(
                    "open_meteo_ensemble",
                    "https://ensemble-api.open-meteo.com/v1/ensemble",
                    "none",
                    None,
                    "fair-use",
                    "Open-Meteo",
                    1800,
                    "authoritative",
                ),
                ProviderSpec(
                    "nws",
                    "https://api.weather.gov",
                    "none",
                    None,
                    "public",
                    "NOAA/NWS",
                    1800,
                    "authoritative",
                ),
                ProviderSpec(
                    "aviation_weather",
                    "https://aviationweather.gov/api/data",
                    "none",
                    None,
                    "public",
                    "NOAA AviationWeather",
                    1800,
                    "authoritative",
                ),
                ProviderSpec(
                    "met_no",
                    "https://api.met.no/weatherapi/locationforecast/2.0",
                    "none",
                    None,
                    "public",
                    "MET Norway",
                    1800,
                    "authoritative",
                ),
                ProviderSpec(
                    "weatherapi",
                    "https://api.weatherapi.com/v1",
                    "api_key",
                    "WEATHERAPI_API_KEY",
                    "free plan",
                    "WeatherAPI",
                    1800,
                    "corroborating",
                ),
                ProviderSpec(
                    "visual_crossing",
                    "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline",
                    "api_key",
                    "VISUAL_CROSSING_API_KEY",
                    "query-cost",
                    "Visual Crossing",
                    1800,
                    "corroborating",
                ),
                ProviderSpec(
                    "tomorrow_io",
                    "https://api.tomorrow.io/v4",
                    "api_key",
                    "TOMORROW_IO_API_KEY",
                    "free plan",
                    "Tomorrow.io",
                    1800,
                    "corroborating",
                ),
                ProviderSpec(
                    "openweather",
                    "https://api.openweathermap.org/data/3.0",
                    "api_key",
                    "OPENWEATHER_API_KEY",
                    "free plan",
                    "OpenWeather",
                    1800,
                    "corroborating",
                ),
                ProviderSpec(
                    "coinbase",
                    "https://api.exchange.coinbase.com",
                    "none",
                    None,
                    "public",
                    "Coinbase Exchange",
                    60,
                    "authoritative",
                ),
                ProviderSpec(
                    "binance",
                    "https://api.binance.com/api/v3",
                    "none",
                    None,
                    "public",
                    "Binance",
                    60,
                    "corroborating",
                ),
                ProviderSpec(
                    "kraken",
                    "https://api.kraken.com/0/public",
                    "none",
                    None,
                    "public",
                    "Kraken",
                    60,
                    "corroborating",
                ),
                ProviderSpec(
                    "reddit",
                    "https://www.reddit.com",
                    "optional",
                    "REDDIT_API_TOKEN",
                    "rate-limited",
                    "Reddit",
                    900,
                    "feature_only",
                ),
                ProviderSpec(
                    "x",
                    "https://api.x.com",
                    "optional",
                    "X_API_TOKEN",
                    "rate-limited",
                    "X",
                    900,
                    "feature_only",
                ),
                ProviderSpec(
                    "github",
                    "https://api.github.com",
                    "optional",
                    "GITHUB_TOKEN",
                    "rate-limited",
                    "GitHub",
                    900,
                    "feature_only",
                ),
            )
        )

    def get(self, name: str) -> ProviderSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {name}") from exc

    def resolve_secret(self, name: str) -> str | None:
        spec = self.get(name)
        if spec.auth == "none":
            return None
        if spec.secret_env is None:
            raise ProviderSecretError(f"{name} has no secret reference")
        value = os.environ.get(spec.secret_env)
        if value:
            return value
        secret_file = os.environ.get(f"{spec.secret_env}_FILE")
        if secret_file:
            value = Path(secret_file).read_text(encoding="utf-8").strip()
            if value:
                return value
        raise ProviderSecretError(f"missing provider secret {spec.secret_env}")

    def health(
        self, name: str, *, retrieved_at: datetime, now: datetime | None = None
    ) -> ProviderHealth:
        spec = self.get(name)
        current = now or datetime.now(UTC)
        age = (current - retrieved_at).total_seconds()
        if age < 0:
            return ProviderHealth(name, False, "clock_skew")
        if age > spec.freshness_seconds:
            return ProviderHealth(name, False, "stale")
        return ProviderHealth(name, True, "ok")


def redact_secrets(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value
