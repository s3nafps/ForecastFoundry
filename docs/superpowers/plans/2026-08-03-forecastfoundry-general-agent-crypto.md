# ForecastFoundry General Quantitative Agent and Crypto Execution Plan

## Objective

Extend the existing paper-only WeatherEdge service into **ForecastFoundry**, a domain-pluggable quantitative prediction-market agent. Weather remains supported, BTC/ETH threshold and up/down markets become the first additional domain, and OpenClaw, Codex CLI, Claude Code, or no external agent can use the same MCP/CLI/API surfaces. Live execution is implemented behind explicit fail-closed gates. Paper mode remains the default and no live order is sent by tests or local startup.

## Architecture and boundaries

- Keep the current FastAPI, APScheduler, SQLAlchemy/Alembic, SQLite, and weather services.
- Add a small domain contract and registry. Each plugin normalizes a market, collects authoritative evidence, produces a probability snapshot, and submits a common signal candidate to the existing edge/risk path.
- Keep `app/services/polymarket.py` for public discovery/order-book reads. Add a separate official Polymarket unified SDK adapter for authenticated execution; never mix private-key code into the public adapter.
- Keep research sources (X, Reddit, GitHub) timestamped and provenance-tagged. They may explain or provide backtest features, but cannot authorize a trade or override resolution evidence.
- Isolate the executor from Hermes MCP. MCP exposes read/research/pause/reconcile tools only; it never receives a wallet key and has no order-signing or risk-mutating tool.
- Keep the MCP contract client-neutral so OpenClaw, Codex CLI, and Claude Code use the same restricted stdio server; add thin configuration examples instead of client-specific business logic.
- Ship stable `forecastfoundry` CLI commands and JSON/OpenAPI responses so scripts and agents can operate without MCP or a particular model vendor.
- Keep the community edition open-source and monetize managed hosting, premium licensed data, enterprise controls, support, and commercial embedding rather than promising returns or taking custody of customer funds.
- Live execution requires both `EXECUTION_ENABLED=true` and an explicit confirmation value, valid caps, an encrypted dedicated keystore, a passing geoblock check, and a fresh revalidation snapshot. Any failure pauses new entries.

## Toolchain

- Python 3.12, existing dependencies, plus the official Polymarket `polymarket-client` SDK selected from `Polymarket/py-sdk`, `cryptography` for the local encrypted keystore, and the official Python MCP SDK for the stdio server.
- Standard-library `math`, `statistics`, `random`, `decimal`, and `zoneinfo` for deterministic crypto calculations; do not add a numerical framework for the first implementation.
- Existing pytest/pytest-asyncio, Ruff, mypy, Alembic, Docker Compose, and the current SQLite test helpers.

## Rebrand and compatibility task

The first implementation task is the ForecastFoundry rebrand and compatibility boundary. Keep the `app` import package, historical Alembic revisions, existing table names, and the `weatheredge.db` default path stable while changing display/package/CLI/Docker names. Preserve old environment names as deprecated aliases until a versioned breaking release.

### 0. Rebrand and add agent-neutral interfaces

Files: `pyproject.toml`, `app/main.py`, `app/__init__.py`, `app/api.py`, `app/dashboard.py`, `app/templates/base.html`, `.env.example`, `docker-compose.yml`, `Dockerfile`, new `app/cli.py`, new `app/mcp_server.py`, new `integrations/openclaw/openclaw.json`, new `integrations/codex/config.toml`, new `integrations/claude/.mcp.json`, new `docs/integrations.md`, `tests/test_branding.py`, `tests/test_cli_contract.py`.

1. Add failing tests for the public product name, `forecastfoundry` distribution/CLI metadata, stable `app` imports, preserved database path, and paper-mode defaults; run `.\.venv\Scripts\python.exe -m pytest tests/test_branding.py tests/test_cli_contract.py -q` and observe red failures.
2. Rename display strings, package metadata, executable name, Docker project/service labels, and documentation to ForecastFoundry. Keep historical storage/table identifiers and add a compatibility version field.
3. Implement the CLI as a thin adapter over existing services with `scan`, `backtest`, `status`, `reconcile`, `pause`, and `mcp` commands; support stable JSON output and no live mode by default.
4. Implement the restricted MCP server once, then add OpenClaw, Codex CLI, and Claude Code configuration examples that invoke the same stdio command. Do not add client-specific domain or execution logic.
5. Run `.\.venv\Scripts\python.exe -m pytest tests/test_branding.py tests/test_cli_contract.py -q` and expect green; run `.\.venv\Scripts\python.exe -m ruff check app/cli.py app/mcp_server.py`.
6. Commit: `git add pyproject.toml app app/templates .env.example docker-compose.yml Dockerfile integrations docs/integrations.md tests/test_branding.py tests/test_cli_contract.py && git commit -m "feat: rebrand as ForecastFoundry and add agent-neutral interfaces"`.

## Implementation tasks

### 1. Add explicit execution configuration and fail-closed policy

Files: `app/config.py`, `.env.example`, `tests/test_config.py`, new `app/services/execution_policy.py`, new `tests/test_execution_policy.py`.

1. Add settings for domain polling, provider keys/secrets paths, `execution_enabled`, `execution_confirmation`, `closed_only`, geoblock endpoint, keystore path, and Hermes MCP enablement. Keep existing weather defaults unchanged.
2. Replace the permanent `real_trading_enabled` rejection with validation that requires `execution_enabled`, the exact confirmation value, balanced limits, a dedicated keystore path, and `closed_only=true` before live mode can start. Invalid or missing live configuration raises a `ValidationError`.
3. Add a pure `ExecutionPolicy`/`assert_startup_safe` function that enforces 5% max trade, 25% aggregate exposure, 10% daily loss, no leverage/martingale/averaging-down, and paper mode by default.
4. Write failing tests for default paper mode, invalid confirmation, cap violations, missing keystore, and a valid paper configuration; run `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_execution_policy.py -q` and observe the new failures.
5. Implement the minimum validators and rerun the same command; expect all tests in those files to pass.
6. Update `.env.example` with safe paper values and comments that no secret or wallet key belongs in environment text.
7. Commit: `git add app/config.py app/services/execution_policy.py tests/test_config.py tests/test_execution_policy.py .env.example && git commit -m "feat: add fail-closed execution policy"`.

### 2. Introduce domain contracts and the plugin registry

Files: new `app/domains/base.py`, new `app/domains/registry.py`, new `app/domains/weather.py`, new `app/domains/crypto.py`, `app/worker.py`, `tests/test_domain_registry.py`.

1. Define typed contracts for normalized market terms, evidence snapshots, prediction runs, domain scan results, and research features. Require market id, resolution source, time semantics, units/quote, freshness, provenance, and ambiguity status.
2. Implement a registry with `weather` and `crypto` entries and a strict router that returns a rejection reason for unknown or ambiguous markets instead of guessing.
3. Wrap the existing temperature normalizer behind the weather plugin without changing its calculations or existing fixtures.
4. Add a narrow crypto registration that only accepts BTC/ETH threshold or up/down terms; it must return `unsupported_market` for other crypto or generic text until the parser is implemented in Task 4.
5. Add registry tests for both plugins, unknown domains, ambiguous terms, and preservation of existing weather routing. Run `.\.venv\Scripts\python.exe -m pytest tests/test_domain_registry.py -q` (red first, then green).
6. Change `app/worker.py` to route discovered markets through the registry while retaining current weather behavior and recording domain rejection reasons.
7. Commit: `git add app/domains app/worker.py tests/test_domain_registry.py && git commit -m "feat: add domain plugin routing"`.

### 3. Build provider registry, credentials, and health checks

Files: new `app/providers/registry.py`, new `app/providers/weather.py`, new `app/providers/crypto.py`, new `app/providers/research.py`, `app/services/http.py`, `app/config.py`, `tests/test_provider_registry.py`, `tests/fixtures/providers/*.json`.

1. Model provider metadata: endpoint, auth mode, quota/rate limit, attribution/license, freshness SLA, and classification (`authoritative`, `corroborating`, `feature_only`).
2. Register keyless Open-Meteo Ensemble, NWS, AviationWeather, MET Norway, Coinbase Exchange public data, Binance public data, and Kraken public data. Register keyed WeatherAPI, Visual Crossing, Tomorrow.io, and OpenWeather only when the user supplies a secret reference; never discover or generate keys.
3. Add strict secret resolution from Docker secret files or configured environment names. Redact keys in logs and provider error records.
4. Add provider health/freshness/quota checks and reuse the existing HTTP retry/circuit-breaker behavior. A stale or inconsistent authoritative source is a hard rejection; cross-provider data is a quality check unless the market names that source.
5. Write fixture-backed tests for registry metadata, missing optional keys, redaction, NWS User-Agent, MET Norway identifying User-Agent, provider timeout, stale data, and rate-limit classification. Run `.\.venv\Scripts\python.exe -m pytest tests/test_provider_registry.py -q`, then implement until green.
6. Commit: `git add app/providers app/services/http.py app/config.py tests/test_provider_registry.py tests/fixtures/providers && git commit -m "feat: add provider registry and health"`.

### 4. Implement strict crypto market normalization

Files: `app/domains/crypto.py`, `app/schemas.py`, new `tests/test_crypto_contracts.py`, `tests/fixtures/markets/crypto/*.json`.

1. Add parser tests first for BTC/ETH, quote currency, exact exchange/index/oracle, threshold/up-down comparison, expiry UTC, candle/price definition, timezone, rounding, and outcome mapping. Include malformed, generic, and conflicting source examples; run the test file to capture red failures.
2. Implement a deterministic parser that only accepts a named source or an explicitly configured source mapping. Reject missing asset, quote, source, threshold, time, price definition, or resolution rules with machine-readable reasons.
3. Normalize all timestamps to UTC and preserve original rules plus field-level provenance. Do not infer an oracle from a title.
4. Add tests for canonical BTC/ETH fixtures, timezone conversion, rounding, and every rejection reason. Run `.\.venv\Scripts\python.exe -m pytest tests/test_crypto_contracts.py -q` and expect green.
5. Commit: `git add app/domains/crypto.py app/schemas.py tests/test_crypto_contracts.py tests/fixtures/markets/crypto && git commit -m "feat: normalize crypto market contracts"`.

### 5. Add deterministic crypto data and probability models

Files: new `app/services/crypto_data.py`, new `app/services/crypto_probability.py`, new `app/services/calibration.py`, `tests/test_crypto_probability.py`, `tests/test_calibration.py`, `tests/fixtures/crypto/*.json`.

1. Add tests for matched-source return series, insufficient history, stale ticks, duplicate/out-of-order candles, empirical bootstrap quantiles, EWMA volatility with zero-drift shrinkage, volatility Monte Carlo, seeded reproducibility, and probability bounds. Run the tests before implementation and record the expected red state.
2. Implement source-aligned price/candle normalization and quality gates. Use the named authoritative source for the prediction; use Coinbase/Binance/Kraken cross-checks only for quality diagnostics.
3. Implement the first model as equal-weight empirical bootstrap plus volatility Monte Carlo, with explicit horizon, seed, sample count, and zero-drift assumptions. Persist every input hash and parameter so a run can be replayed.
4. Add walk-forward calibration with Brier score/reliability buckets. Community X/Reddit/GitHub features are stored separately and remain disabled for live probability until a test proves an improvement over the baseline.
5. Run `.\.venv\Scripts\python.exe -m pytest tests/test_crypto_probability.py tests/test_calibration.py -q`; require deterministic expected values and no NaN/inf output.
6. Commit: `git add app/services/crypto_data.py app/services/crypto_probability.py app/services/calibration.py tests/test_crypto_probability.py tests/test_calibration.py tests/fixtures/crypto && git commit -m "feat: add deterministic crypto probability models"`.

### 6. Extend persistence for evidence, predictions, research, and live state

Files: `app/models.py`, new `alembic/versions/0002_general_agent.py`, `app/database.py`, new `tests/test_general_agent_migration.py`.

1. Add SQLAlchemy models for domain contracts, provider registry rows, evidence snapshots, prediction runs/features/calibration, research documents, execution orders, execution fills/reconciliation events, and kill-switch events. Keep live order tables separate from `paper_positions`.
2. Store only encrypted-keystore metadata (path, fingerprint, created-at, status), never private key material.
3. Add indexes/constraints for market/source/time uniqueness, order idempotency, event ordering, and one active kill switch state.
4. Write a migration test that upgrades an empty database, checks all new tables/constraints, and downgrades cleanly. Run `.\.venv\Scripts\python.exe -m pytest tests/test_general_agent_migration.py -q` to capture red, add the migration, then rerun for green.
5. Run `.\.venv\Scripts\python.exe -m alembic upgrade head` against a temporary SQLite database and `.\.venv\Scripts\python.exe -m alembic downgrade base` as a smoke check.
6. Commit: `git add app/models.py app/database.py alembic/versions/0002_general_agent.py tests/test_general_agent_migration.py && git commit -m "feat: persist general agent evidence and execution state"`.

### 7. Add official Polymarket SDK execution and encrypted keystore

Files: `pyproject.toml`, new `app/services/keystore.py`, new `app/services/polymarket_execution.py`, new `app/services/geoblock.py`, new `tests/test_keystore.py`, new `tests/test_polymarket_execution.py`.

1. Add the official unified `polymarket-client` dependency from `Polymarket/py-sdk`, pin the selected release, and add `cryptography` if it is not already transitively installed. Do not use the archived `py-clob-client` for new code.
2. Write keystore tests first for encrypted-at-rest creation, wrong-password failure, file permission validation on Linux, key fingerprint exposure without secret exposure, and no-key paper mode. Run the test file to confirm red.
3. Implement AES-GCM encrypted keystore load/unlock in a dedicated process boundary. Read the unlock password only from the executor process, keep raw keys out of settings/logging/MCP, and fail closed on permissions, malformed metadata, or wrong password.
4. Add geoblock client and tests for allowed, blocked, timeout, and malformed responses. A failed geoblock check must block live order submission.
5. Wrap the official SDK behind a narrow `ExecutionClient` protocol with `place_marketable_limit`, `cancel_order`, `get_order`, `get_trades`, `get_balance`, and `get_allowance`. Unit tests use a fake SDK and assert that no network call is made in paper mode.
6. Run `.\.venv\Scripts\python.exe -m pytest tests/test_keystore.py tests/test_polymarket_execution.py -q`; then run `.\.venv\Scripts\python.exe -m ruff check app/services/keystore.py app/services/polymarket_execution.py app/services/geoblock.py`.
7. Commit: `git add pyproject.toml app/services/keystore.py app/services/polymarket_execution.py app/services/geoblock.py tests/test_keystore.py tests/test_polymarket_execution.py && git commit -m "feat: add gated Polymarket execution adapter"`.

### 8. Implement shared risk sizing, order state, and reconciliation

Files: new `app/services/risk.py`, new `app/services/execution.py`, new `app/services/reconciliation.py`, `app/services/signals.py`, new `tests/test_risk.py`, new `tests/test_reconciliation.py`.

1. Add failing tests for `raw_edge`, `usable_edge`, uncertainty-adjusted quarter-Kelly sizing, available balance, min order size, 5% trade cap, 25% aggregate cap, 10% daily loss cap, correlated BTC/ETH asset buckets, and every rejection reason. Run the tests red.
2. Implement sizing from calibrated/uncertainty-adjusted probability, then cap by balance, exposure, fees, min size, and policy. Never use leverage, martingale, averaging down, or unbounded market orders.
3. Implement the live sequence: geoblock, closed-only/account checks, balance/allowance, fresh rules/tick/min-size/orderbook, recompute from the same snapshot, bounded marketable limit, sign locally, submit, fetch order/trades, cancel stale remainder, and persist every transition.
4. Implement idempotency keys and reconciliation. Unknown status, partial-fill disagreement, stale order, kill-switch activation, daily loss breach, provider outage, or risk breach pauses new entries and records an auditable event.
5. Run `.\.venv\Scripts\python.exe -m pytest tests/test_risk.py tests/test_reconciliation.py -q`; require all state-machine tests to pass using a fake execution client.
6. Commit: `git add app/services/risk.py app/services/execution.py app/services/reconciliation.py app/services/signals.py tests/test_risk.py tests/test_reconciliation.py && git commit -m "feat: add risk controls and order reconciliation"`.

### 9. Integrate domain scans, prediction persistence, and paper/live decisioning

Files: `app/worker.py`, `app/main.py`, `app/services/paper.py`, new `app/services/agent_scan.py`, new `tests/test_agent_scan.py`, existing weather tests.

1. Add scan tests with recorded weather and BTC fixtures. Assert both domains produce evidence/prediction records, shared signal calculations, and explicit rejection reasons for stale/ambiguous data. Run red first.
2. Implement the scan orchestration: discover markets, route by domain, fetch authoritative evidence, run deterministic models, persist snapshots, evaluate shared signal policy, and pass accepted candidates to paper trading unless every live gate is true.
3. Keep the current weather path and alerts compatible; preserve existing 39 tests as a regression gate.
4. Add scheduler jobs for crypto scans and provider health using existing APScheduler wiring. Make the scheduler the owner of autonomous execution; Hermes must not be required.
5. Run `.\.venv\Scripts\python.exe -m pytest -q` and expect the original tests plus the new scan tests to pass. Run `.\.venv\Scripts\python.exe -m mypy app` and resolve all strict errors.
6. Commit: `git add app/worker.py app/main.py app/services/paper.py app/services/agent_scan.py tests/test_agent_scan.py && git commit -m "feat: route weather and crypto through agent scans"`.

### 10. Add the restricted client-neutral MCP stdio server

Files: `pyproject.toml`, `app/mcp_server.py`, `app/mcp_policy.py`, `tests/test_mcp_server.py`, `config/hermes-mcp.json`, `integrations/openclaw/openclaw.json`, `integrations/codex/config.toml`, `integrations/claude/.mcp.json`, `docs/integrations.md`.

1. Add the official MCP Python dependency and write tests that enumerate the tool surface. The test must fail if `place_order`, `sign_order`, `set_risk_limits`, `wallet_transfer`, arbitrary HTTP, prompts, resources, or parallel calls are exposed.
2. Implement only `list_supported_domains`, `scan_markets`, `get_market_evidence`, `explain_prediction`, `run_backtest`, `provider_health`, `portfolio_status`, `reconcile_orders`, `pause_execution`, and `resume_execution`.
3. Require authenticated/audited pause/resume calls and route them through the local execution policy. MCP has no keystore or executor secret in its environment.
4. Add a stdio launcher and Hermes, OpenClaw, Codex CLI, and Claude Code configurations with an explicit tool allowlist and filtered environment. Keep autonomous scheduling functional when MCP is disabled.
5. Run `.\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q`; run the MCP server in a subprocess with a harmless `list_supported_domains` call and assert clean JSON-RPC shutdown.
6. Commit: `git add pyproject.toml app/mcp_server.py app/mcp_policy.py tests/test_mcp_server.py config/hermes-mcp.json integrations docs/integrations.md && git commit -m "feat: add restricted agent-neutral MCP surface"`.

### 11. Expose status, research provenance, and operator controls

Files: `app/api.py`, `app/schemas.py`, `app/dashboard.py`, `app/templates/overview.html`, `app/templates/market.html`, new `tests/test_api_general_agent.py`, `README.md`.

1. Add API schemas/endpoints for domain support, provider health, latest evidence/prediction, portfolio/exposure, order reconciliation, and kill-switch state. Keep private keys and secret values out of every response.
2. Add a dashboard view for weather and crypto candidates showing source, timestamp, rule status, model probability, executable ask, buffers, rejection reason, and paper/live mode.
3. Add tests for redacted status responses, closed-only live state, provider failures, and pause/resume authorization. Run the file red, implement, then rerun for green.
4. Document the operator workflow, source classifications, calibration requirements, and explicit paper-to-live checklist in `README.md`.
5. Commit: `git add app/api.py app/schemas.py app/dashboard.py app/templates README.md tests/test_api_general_agent.py && git commit -m "feat: expose agent status and operator controls"`.

### 12. Harden Docker deployment and observability

Files: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, new `docker/executor-entrypoint.sh`, new `docker/mcp-entrypoint.sh`, new `tests/test_deployment_config.py`.

1. Add non-root executor and MCP services, named `/data` storage, strict Docker secret mounts, loopback-only HTTP, and separate MCP environment without executor secrets. Keep Nginx optional and outside the default compose file.
2. Add entrypoint checks for migrations, invalid caps, missing keystore, file permissions, geoblock failure, and provider health. Startup must fail closed and record a clear structured error.
3. Add structured audit events for provider failures, rejected candidates, order transitions, pause/resume, daily-loss breach, and emergency cancel-all. Never log passwords, private keys, or API key values.
4. Add deployment-config tests that parse Compose and assert non-root users, secret separation, health checks, and no live mode by default. Run `.\.venv\Scripts\python.exe -m pytest tests/test_deployment_config.py -q`.
5. Run `docker compose config` and `docker build .` (or the local equivalent) as smoke checks, then commit: `git add Dockerfile docker-compose.yml .dockerignore .env.example docker tests/test_deployment_config.py && git commit -m "ops: harden general agent deployment"`.

### 13. Final verification and paper-mode acceptance evidence

Files: `tests/acceptance/`, `docs/superpowers/specs/2026-08-02-weatheredge-general-agent-design.md`, `docs/superpowers/specs/2026-08-03-forecastfoundry-interoperability-and-commercialization-design.md`, `README.md`.

1. Add recorded weather/crypto fixtures and acceptance tests for provider outage, stale source, ambiguous rules, geoblock, closed-only mode, insufficient balance, min-size rejection, partial fill, unknown order status, daily-loss stop, and MCP tool filtering.
2. Run the complete gate:
   `.\.venv\Scripts\python.exe -m pytest -q`
   `.\.venv\Scripts\python.exe -m ruff check .`
   `.\.venv\Scripts\python.exe -m mypy app`
   `.\.venv\Scripts\python.exe -m alembic upgrade head`
   `git diff --check`
3. Run a walk-forward paper calibration using recorded data and save the summary (Brier score, reliability, edge after buffers, drawdown, and rejected-candidate counts) under `docs/evidence/`.
4. Run a mocked SDK signing/order-state test; verify no real order endpoint or private key is touched. Verify `EXECUTION_ENABLED` is false in the default `.env.example` and compose configuration.
5. Scan implementation paths for accidental secret material with `rg -n "PRIVATE_KEY|API_KEY=|BEGIN (RSA|EC|OPENSSH) PRIVATE KEY" --glob "!\.venv/**" app tests config docker`; the command must return no secret material.
6. Review the Polymarket terms, geoblock/jurisdiction, source licenses/attribution, and operator live-trading checklist before enabling any real account.
7. Commit: `git add tests/acceptance docs/evidence README.md && git commit -m "test: add general agent acceptance evidence"`.

### 14. Publish the open-source and commercial boundary

Files: new `LICENSE`, new `NOTICE`, new `SECURITY.md`, new `CONTRIBUTING.md`, new `SUPPORT.md`, `README.md`, new `docs/commercial.md`, new `tests/test_repository_policy.py`.

1. Add repository-policy tests first. Assert an OSI-approved license is present, the README makes no profit/performance guarantee, no customer funds are pooled or held, and the default configuration is paper-only; run `.\.venv\Scripts\python.exe -m pytest tests/test_repository_policy.py -q` and capture red failures.
2. Add Apache-2.0 (preferred) or MIT license text, attribution/NOTICE rules, security reporting, contribution terms, and support boundaries. Do not add a non-commercial restriction to the open-source core.
3. Document community features versus paid hosted/enterprise features: managed deployment, upgrades/backups, tenant controls, licensed premium data, audit exports, support/SLA, and commercial embedding licenses. Keep safety-critical risk and execution code auditable in the community edition.
4. Document that ForecastFoundry does not guarantee returns, provide investment advice, custody user funds, or pool capital. Require jurisdiction-specific legal/compliance review before any custody, managed execution, or revenue-share product.
5. Run `.\.venv\Scripts\python.exe -m pytest tests/test_repository_policy.py -q`, then `.\.venv\Scripts\python.exe -m ruff check .`; expect green and no secret material in policy files.
6. Commit: `git add LICENSE NOTICE SECURITY.md CONTRIBUTING.md SUPPORT.md README.md docs/commercial.md tests/test_repository_policy.py && git commit -m "docs: define open-source and commercial boundaries"`.

## Handoff

After this plan is approved, execute it in this worktree with the `executing-plans` skill. Keep commits small, run the listed red/green checks at each task boundary, and stop before live execution if any security, jurisdiction, geoblock, keystore, reconciliation, or calibration gate is incomplete.
