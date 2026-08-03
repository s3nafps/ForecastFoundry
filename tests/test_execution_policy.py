from pathlib import Path

import pytest

from app.config import Settings
from app.services.execution_policy import (
    ExecutionSafetyError,
    assert_live_order_allowed,
    assert_startup_safe,
)
from app.services.keystore import encrypt_keystore
from app.services.kill_switch import KillSwitch
from app.services.risk import RiskDecision


def live_settings(path: Path) -> Settings:
    return Settings(
        execution_enabled=True,
        execution_confirmation="ENABLE_FORECASTFOUNDRY_LIVE_EXECUTION",
        keystore_path=str(path),
    )


def test_live_mode_requires_an_existing_keystore(tmp_path: Path) -> None:
    with pytest.raises(ExecutionSafetyError, match="keystore"):
        assert_startup_safe(live_settings(tmp_path / "missing.json"))


def test_paper_mode_does_not_require_a_keystore() -> None:
    assert_startup_safe(Settings())


def test_live_mode_accepts_a_strict_keystore_file(tmp_path: Path) -> None:
    keystore = tmp_path / "bot.json"
    encrypt_keystore(keystore, "password", {"private_key": "0xprivate"})

    assert_startup_safe(live_settings(keystore))


def test_live_order_preflight_requires_geoblock_kill_switch_and_risk(tmp_path: Path) -> None:
    keystore = tmp_path / "bot.json"
    encrypt_keystore(keystore, "password", {"private_key": "0xprivate"})
    settings = live_settings(keystore)
    decision = RiskDecision(True, shares=1, notional=1)
    switch = KillSwitch(active=False, reason="")

    assert_live_order_allowed(settings, kill_switch=switch, geoblock_allowed=True, risk=decision)

    with pytest.raises(ExecutionSafetyError, match="geoblock"):
        assert_live_order_allowed(
            settings, kill_switch=switch, geoblock_allowed=False, risk=decision
        )
