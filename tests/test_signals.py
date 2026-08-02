from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas import AlertState, EdgeBuffers, SignalCandidate, SignalPolicy
from app.services.signals import calculate_usable_edge, evaluate_signal, should_send_alert

NOW = datetime(2026, 8, 2, 9, tzinfo=UTC)


def candidate(**changes: object) -> SignalCandidate:
    values: dict[str, object] = {
        "market_id": "3237363",
        "outcome_label": "23°C",
        "generated_at": NOW,
        "market_active": True,
        "market_close_time": NOW + timedelta(hours=3),
        "rules_complete": True,
        "rule_confidence": 95,
        "model_probability": Decimal("0.68"),
        "best_ask": Decimal("0.46"),
        "spread": Decimal("0.03"),
        "liquidity": Decimal("1000"),
        "minimum_order_size": Decimal("5"),
        "paper_balance": Decimal("5.00"),
        "valid_members": 31,
        "observations_required": False,
        "observations_stale": False,
        "critical_quality_flags": (),
    }
    values.update(changes)
    return SignalCandidate.model_validate(values)


BUFFERS = EdgeBuffers(
    estimated_fee=Decimal("0.01"),
    slippage=Decimal("0.01"),
    uncertainty=Decimal("0.04"),
    rule_risk=Decimal("0.02"),
)
POLICY = SignalPolicy(
    min_rule_confidence=90,
    min_ensemble_members=25,
    min_usable_edge=Decimal("0.10"),
    max_spread=Decimal("0.04"),
    min_liquidity=Decimal("100"),
)


def test_usable_edge_subtracts_all_buffers_from_executable_ask() -> None:
    edge = calculate_usable_edge(candidate(), BUFFERS)

    assert edge.raw_edge == Decimal("0.22")
    assert edge.usable_edge == Decimal("0.14")


def test_signal_accepts_only_when_every_gate_passes() -> None:
    decision = evaluate_signal(candidate(), POLICY, BUFFERS)

    assert decision.accepted is True
    assert decision.rejection_reasons == ()


def test_signal_records_every_rejection_reason() -> None:
    decision = evaluate_signal(
        candidate(
            market_active=False,
            market_close_time=NOW,
            rules_complete=False,
            rule_confidence=70,
            best_ask=None,
            spread=Decimal("0.08"),
            liquidity=Decimal("10"),
            minimum_order_size=Decimal("20"),
            valid_members=3,
            observations_required=True,
            observations_stale=True,
            critical_quality_flags=("invalid_member_values",),
        ),
        POLICY,
        BUFFERS,
    )

    assert set(decision.rejection_reasons) == {
        "market_inactive",
        "market_closed",
        "rules_incomplete",
        "rule_confidence_below_minimum",
        "executable_ask_missing",
        "spread_above_maximum",
        "liquidity_below_minimum",
        "minimum_order_exceeds_balance",
        "insufficient_ensemble_members",
        "observations_stale",
        "critical_data_quality_flags",
        "usable_edge_below_minimum",
    }


def test_duplicate_alert_waits_for_material_change_or_cooldown() -> None:
    previous = AlertState(
        outcome_label="23°C",
        executable_ask=Decimal("0.46"),
        model_probability=Decimal("0.68"),
        usable_edge=Decimal("0.14"),
        sent_at=NOW,
    )
    unchanged = previous.model_copy(update={"sent_at": NOW + timedelta(minutes=1)})

    assert should_send_alert(previous, unchanged, now=NOW + timedelta(minutes=10)) is False
    assert (
        should_send_alert(
            previous,
            unchanged.model_copy(update={"executable_ask": Decimal("0.44")}),
            now=NOW + timedelta(minutes=10),
        )
        is True
    )
    assert should_send_alert(previous, unchanged, now=NOW + timedelta(hours=1)) is True
