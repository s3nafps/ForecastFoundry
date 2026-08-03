# ForecastFoundry integrations

ForecastFoundry exposes one restricted MCP server, a JSON-capable CLI, and a local REST/OpenAPI API. The MCP server is the same process for OpenClaw, Codex CLI, Claude Code, and other MCP clients.

Install the project, verify paper mode, and inspect the available commands:

```powershell
python -m pip install -e ".[dev]"
forecastfoundry status --json
forecastfoundry --help
```

The default output must report `PAPER_ONLY`. Do not place wallet keys, API keys, or unlock passwords in these client configuration files. The executor keystore is intentionally unavailable to MCP.

Pause and resume are the only mutating MCP calls. They require the separate `FORECASTFOUNDRY_OPERATOR_TOKEN` and write an audit record; this token is not an executor wallet secret.

## OpenClaw

Merge `integrations/openclaw/openclaw.json` into the node-local MCP configuration, then run `openclaw mcp status --verbose` and `openclaw mcp probe`. Keep the server tool filter limited to the listed research/status/reconciliation/pause tools.

## Codex CLI

Add the `[mcp_servers.forecastfoundry]` block from `integrations/codex/config.toml` to the approved Codex MCP configuration. Start Codex after verifying `forecastfoundry mcp` is on `PATH`.

## Claude Code

Copy `integrations/claude/.mcp.json` to the project root or register the server with `claude mcp add forecastfoundry -- forecastfoundry mcp`. Confirm the server exposes no signer or wallet tool before using it.

## Standalone operation

No external agent is required. The scheduler, CLI, REST API, dashboard, provider registry, model engine, paper ledger, and executor policy run locally. MCP is only an optional control and research client.
