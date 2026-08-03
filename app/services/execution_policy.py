from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services.keystore import KeystoreError, validate_encrypted_keystore
from app.services.kill_switch import KillSwitch, KillSwitchError
from app.services.risk import RiskDecision


class ExecutionSafetyError(ValueError):
    """Raised when a live executor cannot prove its startup safety conditions."""


@dataclass(frozen=True)
class ExecutionPolicy:
    enabled: bool
    max_trade_fraction: str
    max_exposure_fraction: str
    max_daily_loss_fraction: str


def build_execution_policy(settings: Settings) -> ExecutionPolicy:
    return ExecutionPolicy(
        enabled=settings.execution_enabled,
        max_trade_fraction=str(settings.max_trade_fraction),
        max_exposure_fraction=str(settings.max_exposure_fraction),
        max_daily_loss_fraction=str(settings.max_daily_loss_fraction),
    )


def assert_startup_safe(settings: Settings) -> None:
    policy = build_execution_policy(settings)
    if not policy.enabled:
        return
    assert settings.keystore_path is not None
    path = Path(settings.keystore_path)
    try:
        validate_encrypted_keystore(path)
    except KeystoreError as exc:
        raise ExecutionSafetyError(str(exc)) from exc


def assert_live_order_allowed(
    settings: Settings,
    *,
    kill_switch: KillSwitch,
    geoblock_allowed: bool,
    risk: RiskDecision,
) -> None:
    assert_startup_safe(settings)
    if not settings.execution_enabled:
        raise ExecutionSafetyError("live execution is disabled")
    if not geoblock_allowed:
        raise ExecutionSafetyError("geoblock check did not allow execution")
    try:
        kill_switch.assert_clear()
    except KillSwitchError as exc:
        raise ExecutionSafetyError(str(exc)) from exc
    if not risk.approved:
        raise ExecutionSafetyError(f"risk policy rejected order: {', '.join(risk.reasons)}")
