from dataclasses import dataclass
from decimal import Decimal

_KNOWN_STATUSES = {"pending", "open", "partially_filled", "filled", "cancelled", "rejected"}


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    provider_order_id: str
    status: str
    size: Decimal
    filled_size: Decimal


@dataclass(frozen=True)
class ReconciliationResult:
    reconciled: bool
    status: str
    discrepancies: tuple[str, ...] = ()


def reconcile_order(expected: OrderSnapshot, observed: OrderSnapshot) -> ReconciliationResult:
    discrepancies: list[str] = []
    if expected.client_order_id != observed.client_order_id:
        discrepancies.append("client_order_id_mismatch")
    if expected.provider_order_id != observed.provider_order_id:
        discrepancies.append("provider_order_id_mismatch")
    if observed.status not in _KNOWN_STATUSES:
        discrepancies.append("unknown_status")
    if observed.size != expected.size:
        discrepancies.append("order_size_mismatch")
    if observed.filled_size < 0:
        discrepancies.append("negative_filled_size")
    if observed.filled_size > expected.size:
        discrepancies.append("filled_size_exceeds_order")
    return ReconciliationResult(
        reconciled=not discrepancies,
        status=observed.status,
        discrepancies=tuple(discrepancies),
    )
