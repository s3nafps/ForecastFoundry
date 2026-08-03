import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED_TOOLS = (
    "list_supported_domains",
    "scan_markets",
    "get_market_evidence",
    "explain_prediction",
    "run_backtest",
    "provider_health",
    "portfolio_status",
    "reconcile_orders",
    "pause_execution",
    "resume_execution",
)
FORBIDDEN_TOOL_NAMES = {
    "place_order",
    "sign_order",
    "set_risk_limits",
    "wallet_transfer",
    "arbitrary_http",
    "get_private_key",
}


class MCPPolicyError(PermissionError):
    pass


def require_operator(token: str) -> None:
    configured = os.getenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "")
    if not configured or not token or not hmac.compare_digest(token, configured):
        raise MCPPolicyError("authenticated operator token is required")


def audit_operator_action(action: str, reason: str) -> None:
    path = Path(os.getenv("FORECASTFOUNDRY_AUDIT_PATH", "data/mcp-audit.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "action": action,
        "reason": reason,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
