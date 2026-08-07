"""Client-neutral MCP adapter over the shared application services."""

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import COMPATIBILITY_VERSION, PRODUCT_NAME
from app.config import Settings
from app.database import make_engine, make_session_factory
from app.domains.base import MarketInput
from app.mcp_policy import require_operator
from app.models import Base
from app.services.application import ApplicationServices
from app.services.execution_control import ExecutionControlConflict

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


class MCPFacade:
    def __init__(self, services: ApplicationServices | None = None) -> None:
        self._services = services
        self._engine: AsyncEngine | None = None
        self._init_lock = asyncio.Lock()

    async def _get_services(self) -> ApplicationServices:
        if self._services is not None:
            return self._services
        async with self._init_lock:
            if self._services is None:
                settings = Settings(app_env="mcp")
                self._engine = make_engine(settings.database_url)
                if "pytest" in sys.modules:
                    async with self._engine.begin() as connection:
                        await connection.run_sync(Base.metadata.create_all)
                sessions: async_sessionmaker[AsyncSession] = make_session_factory(self._engine)
                self._services = ApplicationServices(sessions, settings)
        assert self._services is not None
        return self._services

    async def list_supported_domains(self) -> dict[str, object]:
        return await (await self._get_services()).list_supported_domains()

    async def scan_markets(self, markets: list[dict[str, Any]] | None = None) -> dict[str, object]:
        inputs = tuple(MarketInput.model_validate(item) for item in (markets or []))
        return await (await self._get_services()).scan_markets(inputs)

    async def get_market_evidence(self, market_id: str) -> dict[str, object]:
        return await (await self._get_services()).get_market_evidence(market_id)

    async def explain_prediction(self, market_id: str) -> dict[str, object]:
        return await (await self._get_services()).explain_prediction(market_id)

    async def run_backtest(self, dataset: str | None = None) -> dict[str, object]:
        return await (await self._get_services()).run_backtest(dataset)

    async def provider_health(self) -> dict[str, object]:
        return await (await self._get_services()).provider_health()

    async def portfolio_status(self) -> dict[str, object]:
        return await (await self._get_services()).portfolio_status()

    async def reconcile_orders(self) -> dict[str, object]:
        return await (await self._get_services()).reconcile_orders()

    async def pause_execution(
        self,
        reason: str,
        request_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        token = os.getenv("FORECASTFOUNDRY_MCP_OPERATOR_TOKEN", "")
        return await (await self._get_services()).set_execution(
            True,
            token=token,
            reason=reason,
            actor="mcp_operator",
            request_id=request_id,
            expected_revision=expected_revision,
            bootstrap_token=token,
        )

    async def resume_execution(
        self,
        reason: str,
        request_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        token = os.getenv("FORECASTFOUNDRY_MCP_OPERATOR_TOKEN", "")
        return await (await self._get_services()).set_execution(
            False,
            token=token,
            reason=reason,
            actor="mcp_operator",
            request_id=request_id,
            expected_revision=expected_revision,
            bootstrap_token=token,
        )


_HANDLERS: WeakKeyDictionary[MCPServer, dict[str, Callable[..., Awaitable[dict[str, object]]]]] = (
    WeakKeyDictionary()
)


def _machine_readable_control_conflicts(
    handler: Callable[..., Awaitable[dict[str, object]]],
) -> Callable[..., Awaitable[object]]:
    @wraps(handler)
    async def wrapped(**arguments: Any) -> object:
        try:
            return await handler(**arguments)
        except ExecutionControlConflict as exc:
            payload = exc.as_dict()
            return CallToolResult(
                content=[TextContent(text=json.dumps(payload, sort_keys=True))],
                structured_content=payload,
                is_error=True,
            )

    return wrapped


def create_server(services: ApplicationServices | None = None) -> MCPServer:
    facade = MCPFacade(services)
    server = MCPServer(name=PRODUCT_NAME, version=COMPATIBILITY_VERSION)
    handlers: dict[str, Callable[..., Awaitable[dict[str, object]]]] = {
        name: getattr(facade, name) for name in ALLOWED_TOOLS
    }
    for name, handler in handlers.items():
        registered = (
            _machine_readable_control_conflicts(handler)
            if name in {"pause_execution", "resume_execution"}
            else handler
        )
        server.add_tool(registered, name=name, description=f"ForecastFoundry {name}")
    _HANDLERS[server] = handlers
    return server


async def invoke_tool(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, object]:
    handlers = _HANDLERS.get(server)
    if handlers is None or name not in handlers:
        raise ValueError(f"unsupported MCP tool: {name}")
    # Compatibility shim for direct Python callers of the pre-header API. The
    # registered MCP schema never exposes this argument; transport clients use
    # the protected process capability instead.
    legacy_token = arguments.pop("token", None)
    if legacy_token is not None and name in {"pause_execution", "resume_execution"}:
        require_operator(legacy_token)
        previous = os.environ.get("FORECASTFOUNDRY_MCP_OPERATOR_TOKEN")
        os.environ["FORECASTFOUNDRY_MCP_OPERATOR_TOKEN"] = legacy_token
        try:
            return await handlers[name](**arguments)
        finally:
            if previous is None:
                os.environ.pop("FORECASTFOUNDRY_MCP_OPERATOR_TOKEN", None)
            else:
                os.environ["FORECASTFOUNDRY_MCP_OPERATOR_TOKEN"] = previous
    return await handlers[name](**arguments)


def main() -> int:
    create_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
