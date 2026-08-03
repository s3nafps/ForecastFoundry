# ForecastFoundry

ForecastFoundry is an open-source, paper-first prediction-market research and execution engine. Weather is the first domain; BTC/ETH threshold and up/down contracts are supported through strict, source-aware contracts. The same local services work standalone or through the CLI, REST/OpenAPI, OpenClaw, Codex CLI, Claude Code, and Hermes-compatible MCP clients.

Forecasts and probabilities are estimates, not financial advice or guarantees. ForecastFoundry does not promise returns, pool capital, or take custody of customer funds. Paper mode is the default and `EXECUTION_ENABLED=false` is required unless an operator deliberately completes every live gate.

## Interfaces

- `forecastfoundry scan|backtest|status|reconcile|pause|mcp`: stable local CLI commands.
- `http://127.0.0.1:8000`: read-only dashboard and `/api/v1` status, evidence, reconciliation, and operator controls.
- `forecastfoundry mcp`: one restricted MCP server for research, status, reconciliation, and audited pause/resume. It exposes no signer, wallet transfer, arbitrary HTTP, or risk-limit mutation tool.
- `integrations/` contains equivalent OpenClaw, Codex CLI, Claude Code, and Hermes configuration examples. They contain no wallet or provider secrets.

## Quick start

Python 3.12+ is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
forecastfoundry status --json
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run `python -m pytest -q` and `python -m ruff check .` before deployment. Docker Compose keeps the HTTP service on loopback, stores the legacy `weatheredge.db` path for compatibility, and runs the MCP service without the executor `.env` or `/data` volume.

## Providers and data

Keyless public providers include Open-Meteo, NWS, AviationWeather, MET Norway, Coinbase, Binance, and Kraken. WeatherAPI, Visual Crossing, Tomorrow.io, OpenWeather, Reddit, X, and GitHub are optional user-supplied integrations; keys are never discovered or generated. Research features remain feature-only until a versioned walk-forward test proves improvement over the deterministic baseline.

Every accepted contract records its named resolution source, quote/units, UTC expiry, price definition, rounding, provenance, and original rules. Missing or conflicting terms are rejected with machine-readable reasons. Evidence stores provider metadata, timestamps, hashes, freshness, quality flags, and attribution.

## Live execution checklist

Live execution is intentionally separate from MCP and dashboard processes. It requires explicit settings, an absolute encrypted AES-GCM keystore, closed-only mode, geoblock approval, an active kill-switch cleared by an operator, risk caps, and order reconciliation. The official Polymarket SDK is an optional `forecastfoundry[live]` dependency. Review jurisdiction, Polymarket terms, source licenses, and the operator checklist before using real funds.

## Open core and paid services

The community edition includes contracts, providers, deterministic models, calibration, paper ledger, risk/reconciliation code, CLI, REST, MCP, Docker, tests, and fixtures under Apache-2.0. Optional paid services can provide managed deployment, backups, monitoring, tenant controls, licensed premium data, audit exports, support/SLA, and commercial embedding licenses. Safety-critical risk and execution code remains auditable in the community edition.

See [commercial boundaries](docs/commercial.md), [integrations](docs/integrations.md), [security policy](SECURITY.md), and [support](SUPPORT.md).
