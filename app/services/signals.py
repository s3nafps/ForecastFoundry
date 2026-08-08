from decimal import Decimal

from app.schemas import (
    EdgeBuffers,
    EdgeResult,
    SignalCandidate,
    SignalDecision,
    SignalPolicy,
)


def calculate_usable_edge(candidate: SignalCandidate, buffers: EdgeBuffers) -> EdgeResult:
    if candidate.best_ask is None:
        return EdgeResult(raw_edge=None, usable_edge=None)
    raw_edge = candidate.model_probability - candidate.best_ask
    usable_edge = raw_edge - sum(
        (
            buffers.estimated_fee,
            buffers.slippage,
            buffers.uncertainty,
            buffers.rule_risk,
        ),
        Decimal("0"),
    )
    return EdgeResult(raw_edge=raw_edge, usable_edge=usable_edge)


def evaluate_signal(
    candidate: SignalCandidate,
    policy: SignalPolicy,
    buffers: EdgeBuffers,
) -> SignalDecision:
    reasons: list[str] = []
    if not candidate.market_active:
        reasons.append("market_inactive")
    if candidate.market_close_time is None or candidate.market_close_time <= candidate.generated_at:
        reasons.append("market_closed")
    if not candidate.rules_complete:
        reasons.append("rules_incomplete")
    if candidate.rule_confidence < policy.min_rule_confidence:
        reasons.append("rule_confidence_below_minimum")
    if candidate.best_ask is None:
        reasons.append("executable_ask_missing")
    if candidate.spread is None or candidate.spread > policy.max_spread:
        reasons.append("spread_above_maximum")
    if candidate.liquidity < policy.min_liquidity:
        reasons.append("liquidity_below_minimum")
    if candidate.minimum_order_size is None:
        reasons.append("minimum_order_size_missing")
    else:
        minimum_cost = candidate.minimum_order_size * (
            candidate.best_ask if candidate.best_ask is not None else Decimal("1")
        )
        if minimum_cost > candidate.paper_balance:
            reasons.append("minimum_order_exceeds_balance")
    if candidate.valid_members < policy.min_ensemble_members:
        reasons.append("insufficient_ensemble_members")
    if candidate.critical_quality_flags:
        reasons.append("critical_data_quality_flags")

    edge = calculate_usable_edge(candidate, buffers)
    if edge.usable_edge is None or edge.usable_edge < policy.min_usable_edge:
        reasons.append("usable_edge_below_minimum")
    return SignalDecision(
        accepted=not reasons,
        rejection_reasons=tuple(reasons),
        raw_edge=edge.raw_edge,
        usable_edge=edge.usable_edge,
    )
