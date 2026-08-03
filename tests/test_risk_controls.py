from decimal import Decimal

import pytest

from app.services.kill_switch import KillSwitch, KillSwitchError
from app.services.risk import RiskLimits, size_order

LIMITS = RiskLimits(
    max_trade_fraction=Decimal("0.05"),
    max_exposure_fraction=Decimal("0.25"),
    max_daily_loss_fraction=Decimal("0.10"),
)


def test_order_sizing_applies_trade_and_exposure_caps() -> None:
    decision = size_order(
        balance=Decimal("1000"),
        price=Decimal("0.50"),
        requested_shares=Decimal("200"),
        current_exposure=Decimal("100"),
        daily_pnl=Decimal("0"),
        minimum_order_size=Decimal("5"),
        limits=LIMITS,
    )

    assert decision.approved is True
    assert decision.notional == Decimal("50")
    assert decision.shares == Decimal("100")


def test_order_sizing_fails_closed_on_loss_cap_or_minimum() -> None:
    loss = size_order(
        balance=Decimal("1000"),
        price=Decimal("0.50"),
        requested_shares=Decimal("10"),
        current_exposure=Decimal("0"),
        daily_pnl=Decimal("-100"),
        minimum_order_size=Decimal("5"),
        limits=LIMITS,
    )
    assert loss.approved is False
    assert "daily_loss_cap" in loss.reasons

    with pytest.raises(ValueError, match="minimum"):
        size_order(
            balance=Decimal("1000"),
            price=Decimal("0.50"),
            requested_shares=Decimal("1"),
            current_exposure=Decimal("0"),
            daily_pnl=Decimal("0"),
            minimum_order_size=Decimal("5"),
            limits=LIMITS,
        )


def test_kill_switch_blocks_until_explicitly_cleared() -> None:
    switch = KillSwitch()
    with pytest.raises(KillSwitchError, match="active"):
        switch.assert_clear()
    switch.clear()
    switch.assert_clear()
    switch.activate("manual stop")
    with pytest.raises(KillSwitchError, match="manual stop"):
        switch.assert_clear()
