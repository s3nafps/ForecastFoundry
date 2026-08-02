# ForecastFoundry Interoperability and Commercialization Design

## Change summary

This change renames the product from WeatherEdge to **ForecastFoundry** and makes the system agent-neutral. Weather remains the first domain, but the public identity and interfaces no longer imply weather-only behavior or a Polymarket-only client. The existing general quantitative-agent design remains the execution and safety authority; this document adds compatibility, packaging, licensing, and commercialization requirements.

### Requirement deltas

- **RENAMED:** WeatherEdge -> ForecastFoundry for display name, Python distribution, CLI, Docker project, documentation, and public examples.
- **MODIFIED:** Keep the `app` import package and database schema/table names stable for the first migration so existing installations can upgrade without data loss.
- **ADDED:** One standard MCP server usable from OpenClaw, Codex CLI, and Claude Code.
- **ADDED:** A standalone CLI and OpenAPI surface so agents can use the system without a particular model vendor.
- **ADDED:** Open-source core plus paid hosted/enterprise surfaces; no customer-wallet custody or profit guarantees.

## Product identity and compatibility

The canonical display name is ForecastFoundry. Use `forecastfoundry` for the Python distribution, command-line executable, Docker Compose project, service labels, and new documentation. Keep `app` as the import package and preserve existing database table names, migration history, and unprefixed environment names as compatibility aliases during the first release.

The public contract has three equivalent entry points:

1. **MCP stdio server:** the preferred integration for agent clients. It exposes the restricted research/status/control tool set already approved in the general-agent design. It never exposes signing, wallet transfer, arbitrary HTTP, risk-limit mutation, or keystore access.
2. **CLI:** `forecastfoundry scan`, `forecastfoundry backtest`, `forecastfoundry status`, `forecastfoundry reconcile`, `forecastfoundry pause`, and `forecastfoundry mcp`. Commands emit stable JSON with `--json` and human-readable output otherwise. Paper mode is the default.
3. **REST/OpenAPI:** loopback by default for local installs and authenticated HTTPS only for hosted installs. The API is the source for dashboards and remote automation, not a second execution engine.

Client configuration is documentation and examples only; client-specific business logic is prohibited. Ship:

- `integrations/openclaw/openclaw.json` with a local stdio MCP definition and an allowlist;
- `integrations/codex/config.toml` with the equivalent MCP command;
- `integrations/claude/.mcp.json` with the equivalent MCP command;
- `docs/integrations.md` containing install, status, pause, and paper-mode examples for all three clients.

The executor owns the scheduler and risk policy whether or not any MCP client is connected. OpenClaw, Codex CLI, and Claude Code are interchangeable clients, not authorities.

## Naming and migration rules

- Update product text, FastAPI title, Telegram prefix, dashboard title, package metadata, Docker labels, and Compose project name to ForecastFoundry.
- Keep `REAL_TRADING_ENABLED` as a deprecated, rejected compatibility alias until the new explicit execution settings are in place; then document the new fail-closed settings and remove the legacy alias only in a versioned breaking release.
- Keep the existing `weatheredge.db` default path and named data volume for the first release unless an operator explicitly changes `DATABASE_URL`; this avoids a silent data fork during the brand migration.
- Add a startup log and API field showing `product_name=ForecastFoundry` and `compatibility_version=weatheredge-v1` while the aliases exist.
- Do not rename historical Alembic revisions or existing SQL tables solely for branding.

## Open-source and commercial boundaries

### Community edition

Publish the engine under an OSI-approved permissive license, preferably Apache-2.0 for explicit patent language or MIT for maximum adoption. Include:

- domain contracts and weather/crypto plugins;
- provider adapters that can legally be redistributed;
- deterministic models, calibration, backtesting, paper ledger, and replay fixtures;
- risk calculations, execution state machine, reconciliation, and the restricted MCP server;
- CLI, local REST/OpenAPI, Docker Compose, integration examples, tests, and documentation.

Do not add a “non-commercial use only” restriction to code called open source. That would conflict with the intended open-source distribution model.

### Paid value

The commercial product is an optional hosted or supported layer, not a hidden safety-critical algorithm:

- managed VPS/cloud deployment with upgrades, backups, health monitoring, and incident response;
- tenant isolation, SSO/RBAC, audit export, retention policies, and team dashboards;
- managed provider credentials and licensed premium data feeds where redistribution is not permitted;
- encrypted execution service operated in the customer’s account or a customer-controlled deployment;
- calibration reports, historical datasets, and support/SLA packages;
- commercial embedding or redistribution licenses for customers that cannot use the community license.

The hosted service must not take custody of customer funds, pool user capital, promise returns, or charge a percentage of trading profit in the first commercial release. Any later custody, managed execution, or revenue-share product requires separate jurisdiction-specific legal and compliance review.

### Ownership and contributions

Add copyright headers, `LICENSE`, `NOTICE`, `SECURITY.md`, `CONTRIBUTING.md`, and `SUPPORT.md`. Start with a Developer Certificate of Origin for community contributions. Introduce a contributor license agreement only if ForecastFoundry later needs dual licensing of contributed code; make that decision explicit before accepting contributions under different terms.

Reserve ForecastFoundry as a trademark only after a formal clearance search. The name in this document is a product-design decision, not a claim of trademark availability.

## Security and trust boundaries

- MCP, CLI, and REST all call the same local policy and execution service; none may bypass it.
- The executor keystore is never mounted into MCP, API, dashboard, or research containers.
- Default configuration is paper mode with live execution disabled.
- Integration examples contain no wallet key, API key, or password.
- Every action exposed through an agent client is authenticated where required, auditable, idempotent, and subject to pause/kill-switch state.
- Research documents and LLM explanations remain non-authoritative evidence; they cannot change resolution rules, risk limits, or order authorization.

## Acceptance evidence

- All three integration examples start the same MCP server and list the same restricted tools.
- CLI and REST return identical domain/status/reconciliation records for a recorded fixture.
- No integration path exposes a signer, private key, wallet transfer, arbitrary HTTP, or risk-limit mutation.
- A fresh install reports ForecastFoundry while an upgraded WeatherEdge database continues to open with the same rows and Alembic history.
- Default Compose and `.env.example` remain paper-only.
- The repository contains an OSI-approved license, contribution/support/security policies, provider attribution, and no secret material.
- Commercial documentation clearly separates open-source self-hosting from paid hosted/support services and makes no performance or profit guarantee.
