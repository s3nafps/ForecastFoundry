from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_real_trading_can_never_be_enabled() -> None:
    with pytest.raises(ValidationError, match="permanently disabled"):
        Settings(real_trading_enabled=True)


def test_paper_balance_defaults_to_five_dollars() -> None:
    assert Settings().paper_starting_balance == Decimal("5.00")
