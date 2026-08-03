from pathlib import Path

import pytest

from app.config import Settings
from app.services.execution_policy import ExecutionSafetyError, assert_startup_safe
from app.services.keystore import encrypt_keystore


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
