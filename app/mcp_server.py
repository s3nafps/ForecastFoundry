from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from app import COMPATIBILITY_VERSION, PRODUCT_NAME
from app.domains.base import MarketInput
from app.domains.registry import DomainRegistry
from app.mcp_policy import ALLOWED_TOOLS, audit_operator_action, require_operator


class MCPFacade:
    def __init__(self) -> None:
        self._paused = False
        self._registry = DomainRegistry()

    async def list_supported_domains(self) -> dict[str, object]:
        return {
            "product_name": PRODUCT_NAME,
            "compatibility_version": COMPATIBILITY_VERSION,
            "domains": ["weather", "crypto"],
            "mode": "PAPER_ONLY",
        }

    async def scan_markets(
        self, markets: list[dict[str, str]] | None = None
    ) -> dict[str, object]:
        inputs = tuple(
            MarketInput.model_validate(item)
            for item in (markets or [])
            if isinstance(item, dict)
        )
        routes = tuple(self._registry.route(item) for item in inputs)
        return {
            "markets": [
                {
                    "market_id": item.market_id,
                    "accepted": route.accepted,
                    "domain": route.domain,
                    "reasons": list(route.reasons),
                }
                for item, route in zip(inputs, routes, strict=True)
            ],
            "mode": "PAPER_ONLY",
        }

    async def get_market_evidence(self, market_id: str) -> dict[str, object]:
        return {"market_id": market_id, "status": "not_loaded", "secret_values": False}

    async def explain_prediction(self, market_id: str) -> dict[str, object]:
        return {
            "market_id": market_id,
            "status": "not_loaded",
            "explanation": "No prediction is loaded for this market.",
        }

    async def run_backtest(self, market_ids: list[str] | None = None) -> dict[str, object]:
        return {
            "market_ids": market_ids or [],
            "status": "paper_only",
            "predictions": 0,
            "note": "Recorded fixtures or an operator-selected dataset are required.",
        }

    async def provider_health(self) -> dict[str, object]:
        return {"providers": [], "status": "unknown", "secret_values": False}

    async def portfolio_status(self) -> dict[str, object]:
        return {
            "mode": "PAPER_ONLY",
            "paused": self._paused,
            "live_execution": False,
            "wallet_custody": False,
        }

    async def reconcile_orders(self) -> dict[str, object]:
        return {"mode": "PAPER_ONLY", "orders": [], "reconciled": True}

    async def pause_execution(self, token: str, reason: str) -> dict[str, object]:
        require_operator(token)
        if not reason.strip():
            raise ValueError("pause reason is required")
        self._paused = True
        audit_operator_action("pause_execution", reason)
        return {"paused": True, "reason": reason}

    async def resume_execution(self, token: str, reason: str) -> dict[str, object]:
        require_operator(token)
        if not reason.strip():
            raise ValueError("resume reason is required")
        self._paused = False
        audit_operator_action("resume_execution", reason)
        return {"paused": False, "reason": reason}


def create_server() -> MCPServer:
    facade = MCPFacade()
    server = MCPServer(name=PRODUCT_NAME, version="0.1.0")
    handlers: dict[str, Callable[..., Awaitable[dict[str, object]]]] = {
        name: getattr(facade, name) for name in ALLOWED_TOOLS
    }
    for name, handler in handlers.items():
        server.add_tool(handler, name=name, description=f"ForecastFoundry {name}")
    server._forecastfoundry_handlers = handlers  # type: ignore[attr-defined]
    return server


async def invoke_tool(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, object]:
    handlers = server._forecastfoundry_handlers  # type: ignore[attr-defined]
    if name not in handlers:
        raise ValueError(f"unsupported MCP tool: {name}")
    return await handlers[name](**arguments)


def main() -> int:
    create_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
