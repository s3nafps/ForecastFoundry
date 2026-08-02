from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_real_trading_can_never_be_enabled() -> None:
    with pytest.raises(ValidationError, match="explicit execution"):
        Settings(real_trading_enabled=True)


def test_paper_balance_defaults_to_five_dollars() -> None:
    assert Settings().paper_starting_balance == Decimal("5.00")


def test_execution_is_disabled_by_default() -> None:
    settings = Settings()

    assert settings.execution_enabled is False
    assert settings.closed_only is True


def test_live_execution_requires_confirmation() -> None:
    with pytest.raises(ValidationError, match="confirmation"):
        Settings(execution_enabled=True)


def test_live_execution_requires_closed_only_and_keystore(tmp_path) -> None:
    with pytest.raises(ValidationError, match="closed_only"):
        Settings(
            execution_enabled=True,
            execution_confirmation="ENABLE_FORECASTFOUNDRY_LIVE_EXECUTION",
            closed_only=False,
            keystore_path=str(tmp_path / "bot.json"),
        )


def test_valid_live_configuration_is_explicit(tmp_path) -> None:
    settings = Settings(
        execution_enabled=True,
        execution_confirmation="ENABLE_FORECASTFOUNDRY_LIVE_EXECUTION",
        keystore_path=str(tmp_path / "bot.json"),
    )

    assert settings.execution_enabled is True
