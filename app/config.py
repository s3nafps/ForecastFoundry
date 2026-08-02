from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    app_env: str = "production"
    database_url: str = "sqlite+aiosqlite:///./data/weatheredge.db"
    telegram_bot_token: SecretStr | None = None
    telegram_admin_user_id: int | None = None

    polymarket_poll_seconds: int = Field(default=120, gt=0)
    weather_poll_seconds: int = Field(default=900, gt=0)
    observation_poll_seconds: int = Field(default=300, gt=0)
    min_rule_confidence: int = Field(default=90, ge=0, le=100)
    min_ensemble_members: int = Field(default=25, gt=0)
    min_usable_edge: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    max_spread: Decimal = Field(default=Decimal("0.04"), ge=0, le=1)
    min_liquidity_usd: Decimal = Field(default=Decimal("100"), ge=0)
    estimated_fee: Decimal = Field(default=Decimal("0.00"), ge=0, le=1)
    slippage_buffer: Decimal = Field(default=Decimal("0.01"), ge=0, le=1)
    uncertainty_buffer: Decimal = Field(default=Decimal("0.04"), ge=0, le=1)
    rule_risk_buffer: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    paper_starting_balance: Decimal = Field(default=Decimal("5.00"), gt=0)
    forecast_models: str = "gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global"
    station_config_path: str = "config/stations.yaml"
    market_overrides_path: str = "config/market_overrides.yaml"

    real_trading_enabled: bool = False
    execution_enabled: bool = False
    execution_confirmation: SecretStr | None = None
    closed_only: bool = True
    keystore_path: str | None = None
    max_trade_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=Decimal("0.05"))
    max_exposure_fraction: Decimal = Field(default=Decimal("0.25"), gt=0, le=Decimal("0.25"))
    max_daily_loss_fraction: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.10"))
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    hermes_mcp_enabled: bool = True
    polymarket_websocket_enabled: bool = False
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    open_meteo_api_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
    http_timeout_seconds: float = Field(default=20.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0, le=10)
    provider_circuit_failures: int = Field(default=5, gt=0)
    provider_circuit_reset_seconds: int = Field(default=300, gt=0)
    signal_cooldown_seconds: int = Field(default=3600, ge=0)
    material_price_change: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    material_probability_change: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "Settings":
        if self.real_trading_enabled:
            raise ValueError("real trading requires explicit execution settings")
        if not self.execution_enabled:
            return self
        confirmation = (
            self.execution_confirmation.get_secret_value() if self.execution_confirmation else None
        )
        if confirmation != "ENABLE_FORECASTFOUNDRY_LIVE_EXECUTION":
            raise ValueError("execution confirmation is required")
        if not self.closed_only:
            raise ValueError("closed_only must be true for live execution")
        if not self.keystore_path:
            raise ValueError("keystore_path is required for live execution")
        if not Path(self.keystore_path).is_absolute():
            raise ValueError("keystore_path must be absolute for live execution")
        return self
