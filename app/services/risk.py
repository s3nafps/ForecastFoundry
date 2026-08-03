from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class RiskLimits:
    max_trade_fraction: Decimal
    max_exposure_fraction: Decimal
    max_daily_loss_fraction: Decimal

    def __post_init__(self) -> None:
        for value in (
            self.max_trade_fraction,
            self.max_exposure_fraction,
            self.max_daily_loss_fraction,
        ):
            if not 0 < value <= 1:
                raise ValueError("risk limits must be between zero and one")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    shares: Decimal
    notional: Decimal
    reasons: tuple[str, ...] = ()


def size_order(
    *,
    balance: Decimal,
    price: Decimal,
    requested_shares: Decimal,
    current_exposure: Decimal,
    daily_pnl: Decimal,
    minimum_order_size: Decimal,
    limits: RiskLimits,
    unrealized_pnl: Decimal = Decimal("0"),
    asset_exposure: Decimal = Decimal("0"),
    correlated_exposure: Decimal = Decimal("0"),
    fees: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
    data_stale: bool = False,
) -> RiskDecision:
    if (
        balance <= 0
        or price <= 0
        or price > 1
        or requested_shares <= 0
        or current_exposure < 0
        or asset_exposure < 0
        or correlated_exposure < 0
        or fees < 0
        or slippage < 0
    ):
        raise ValueError("risk inputs are invalid")
    if minimum_order_size <= 0:
        raise ValueError("minimum order size must be positive")
    if requested_shares < minimum_order_size:
        raise ValueError("requested order is below market minimum")

    if data_stale:
        return RiskDecision(False, Decimal("0"), Decimal("0"), ("stale_data",))
    loss_cap = balance * limits.max_daily_loss_fraction
    if daily_pnl + unrealized_pnl - fees - slippage <= -loss_cap:
        return RiskDecision(False, Decimal("0"), Decimal("0"), ("daily_loss_cap",))

    trade_cap = balance * limits.max_trade_fraction
    exposure_room = balance * limits.max_exposure_fraction - max(
        current_exposure, asset_exposure + correlated_exposure
    )
    if exposure_room <= 0:
        return RiskDecision(False, Decimal("0"), Decimal("0"), ("exposure_cap",))

    allowed_notional = min(trade_cap, exposure_room) - fees - slippage
    shares = min(requested_shares, allowed_notional / price)
    if shares < minimum_order_size:
        return RiskDecision(False, Decimal("0"), Decimal("0"), ("risk_cap_below_minimum",))
    shares = shares.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return RiskDecision(True, shares, shares * price)
