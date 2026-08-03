from decimal import Decimal

from app.services.reconciliation import OrderSnapshot, reconcile_order


def test_reconciliation_accepts_matching_partial_fill() -> None:
    result = reconcile_order(
        OrderSnapshot("client-1", "provider-1", "open", Decimal("100"), Decimal("0")),
        OrderSnapshot("client-1", "provider-1", "partially_filled", Decimal("100"), Decimal("25")),
    )

    assert result.reconciled is True
    assert result.status == "partially_filled"


def test_reconciliation_never_accepts_unknown_or_overfilled_orders() -> None:
    unknown = reconcile_order(
        OrderSnapshot("client-1", "provider-1", "open", Decimal("100"), Decimal("0")),
        OrderSnapshot("client-1", "provider-1", "mystery", Decimal("100"), Decimal("0")),
    )
    overfilled = reconcile_order(
        OrderSnapshot("client-1", "provider-1", "open", Decimal("100"), Decimal("0")),
        OrderSnapshot("client-1", "provider-1", "filled", Decimal("100"), Decimal("101")),
    )

    assert unknown.reconciled is False
    assert "unknown_status" in unknown.discrepancies
    assert overfilled.reconciled is False
    assert "filled_size_exceeds_order" in overfilled.discrepancies
