import asyncio
import json
import os
import subprocess
import sys

import pytest

from app.mcp_policy import ALLOWED_TOOLS, FORBIDDEN_TOOL_NAMES, MCPPolicyError
from app.mcp_server import create_server, invoke_tool
from app.services.execution_control import IdempotencyConflict


@pytest.mark.asyncio
async def test_mcp_exposes_only_restricted_tools_without_prompts_or_resources() -> None:
    server = create_server()
    names = {tool.name for tool in await server.list_tools()}

    assert names == set(ALLOWED_TOOLS)
    assert names.isdisjoint(FORBIDDEN_TOOL_NAMES)
    assert await server.list_prompts() == []
    assert await server.list_resources() == []
    assert await server.list_resource_templates() == []


def test_pause_resume_require_operator_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "operator-secret")
    server = create_server()

    with pytest.raises(MCPPolicyError, match="authenticated"):
        asyncio.run(invoke_tool(server, "pause_execution", {"token": "wrong", "reason": "test"}))
    paused = asyncio.run(
        invoke_tool(server, "pause_execution", {"token": "operator-secret", "reason": "test"})
    )
    resumed = asyncio.run(
        invoke_tool(server, "resume_execution", {"token": "operator-secret", "reason": "test"})
    )

    assert paused["paused"] is True
    assert resumed["paused"] is False


def test_mcp_raises_typed_idempotency_conflict(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("FORECASTFOUNDRY_MCP_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv(
        "FORECASTFOUNDRY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mcp-control.db'}"
    )
    server = create_server()
    asyncio.run(
        invoke_tool(
            server,
            "resume_execution",
            {"reason": "operator test", "request_id": "mcp-control-1"},
        )
    )
    with pytest.raises(IdempotencyConflict) as raised:
        asyncio.run(
            invoke_tool(
                server,
                "pause_execution",
                {"reason": "operator test", "request_id": "mcp-control-1"},
            )
        )
    assert raised.value.as_dict()["type"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_registered_mcp_tool_returns_machine_readable_idempotency_conflict(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORECASTFOUNDRY_MCP_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv(
        "FORECASTFOUNDRY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'mcp-transport.db'}"
    )
    server = create_server()
    await server.call_tool(
        "resume_execution", {"reason": "operator test", "request_id": "mcp-transport-1"}
    )

    conflict = await server.call_tool(
        "pause_execution", {"reason": "operator test", "request_id": "mcp-transport-1"}
    )

    assert conflict.is_error is True
    assert conflict.structured_content == {
        "type": "idempotency_conflict",
        "message": "request ID is already bound to a different execution control request",
        "request_id": "mcp-transport-1",
    }


def test_mcp_stdio_lists_tools_without_executor_secrets() -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    environment = os.environ.copy()
    environment["EXECUTION_ENABLED"] = "false"
    for key in tuple(environment):
        if "KEY" in key or "SECRET" in key or "PASSWORD" in key:
            environment.pop(key)
    process = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(requests[0]) + "\n")
    process.stdin.flush()
    initialize_response = process.stdout.readline()
    assert '"id":1' in initialize_response
    process.stdin.write("\n".join(json.dumps(request) for request in requests[1:]) + "\n")
    process.stdin.close()
    remaining_stdout = process.stdout.read()
    process.wait(timeout=10)
    stdout = initialize_response + remaining_stdout
    assert process.returncode == 0
    assert "place_order" not in stdout
    assert "list_supported_domains" in stdout
