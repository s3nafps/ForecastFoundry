# ForecastFoundry production hardening plan

## Objective

Turn the current ForecastFoundry prototype into a coherent, paper-first
platform whose CLI, REST API, MCP server, scheduler, dashboard, and tests all
call the same application services. Keep the existing weather schema and
migrations compatible, add a strict BTC/ETH paper path, and make every live
execution boundary fail closed. No live orders, credentials, private keys,
operator tokens, or runtime databases belong in the repository or tests.

## Constraints and acceptance gates

- SQLite remains the default and existing `weatheredge.db` identifiers remain
  compatible; PostgreSQL is an explicitly supported deployment target.
- Paper mode is the default. The executor is the only component allowed to
  hold an unlocked keystore, and tests use fake exchange adapters only.
- Evidence, predictions, controls, and audit records are persisted and
  redacted at every external boundary.
- A failing security, migration, reconciliation, or paper-mode test blocks
  completion. The final audit must list disabled features and residual risks.

## Work order

### 1. Shared service boundary and authoritative execution control

Files: `app/services/application.py`, `app/services/kill_switch.py`,
`app/services/operator_auth.py`, `app/models.py`, a new Alembic revision,
`app/schemas.py`, and focused tests.

1. Add failing tests proving that CLI, REST, MCP, and scheduler can share one
   service object, that no interface returns placeholder data, and that control
   state survives a new process.
2. Add a small typed application-service facade for domain discovery, scans,
   evidence, explanations, backtests, portfolio status, reconciliation,
   provider health, and pause/resume. Keep adapters thin and inject the
   SQLAlchemy session, registry, and provider/exchange implementations.
3. Add a durable control-state row with an optimistic revision, request ID,
   actor, reason, previous/new state, timestamp, and idempotent transitions.
   Scheduler and executor read it immediately before work; a stale revision
   or kill event rejects new entries.
4. Add operator-token hashing, constant-time verification, expiry, rotation,
   permissions, bounded failed-attempt throttling, and redacted audit output.
   REST reads `Authorization: Bearer`; MCP receives capability from protected
   process configuration and never from tool arguments.
5. Run focused tests, Alembic upgrade/downgrade smoke checks, Ruff, and mypy;
   commit the service/control slice.

### 2. Real CLI and MCP adapters

Files: `app/cli.py`, `app/api.py`, `app/mcp_server.py`, `app/mcp_policy.py`,
new CLI/MCP integration tests, and client configuration examples.

1. Replace constant CLI payloads with service calls. Add explicit arguments
   for domain, market, dataset, reason, and `resume`; validate before opening
   resources; emit stable JSON or human output and non-zero failures.
2. Route every MCP tool through the same facade. Keep the allowlist and reject
   order/sign/wallet/HTTP tools, unknown args, and parallel state mutation.
3. Move REST operator auth to the header and return redacted typed responses.
   Keep autonomous scheduler operation valid when MCP is disabled.
4. Add subprocess/stdin tests for a harmless MCP call and command-level tests
   for success, rejection, and unavailable-provider exit codes.

### 3. Strict domain contracts and paper pipelines

Files: `app/domains/weather.py`, `app/domains/crypto.py`, existing detailed
weather rules, provider clients/registry, `app/services/agent_scan.py`,
`app/services/crypto_data.py`, `app/services/crypto_probability.py`,
`app/services/paper.py`, models/migration, and fixtures/tests.

1. Make weather normalization reuse `normalize_temperature_event` and require
   source, location, timezone, units, buckets, rounding/reporting semantics,
   fallback behavior, and provenance. Reject ambiguity with machine-readable
   reasons; never use a free-form description as a source.
2. Implement public, keyless Coinbase/Binance/Kraken candle acquisition with
   timeouts, cache/freshness checks, source alignment, and explicit
   unavailable/rate-limit states. Keyed weather providers are optional and
   never guessed or generated.
3. Complete BTC/ETH threshold and up/down discovery, strict parsing,
   authoritative evidence, deterministic seeded probability, signal creation,
   paper position creation, settlement, and calibration persistence.
4. Persist contract/evidence/prediction references and expose real
   `get_market_evidence` and `explain_prediction` results. Integrate both
   domains into scheduler scans while retaining weather compatibility.

### 4. Genuine temporal backtesting

Files: new `app/services/backtest.py`, schemas, fixtures, tests, and evidence
documentation.

1. Add immutable timestamped dataset loading and expanding/rolling chronological
   splits. Reject leakage, duplicate timestamps, insufficient history, and
   post-resolution features.
2. Report Brier score, log loss, calibration/reliability, sharpness, P&L,
   drawdown, turnover, fees, slippage, trades, and rejection counts with
   deterministic seeded confidence intervals.
3. Compare an explicit baseline before promoting a model; persist dataset and
   parameter hashes for replay.

### 5. Dedicated executor, risk, health, and observability

Files: executor service/module, risk/reconciliation services, provider health
store, structured logging/metrics, Docker configuration, and tests.

1. Add a separate executor process and fake exchange protocol. Re-check
   control state, geoblock, closed-only, balance, allowance, rules, tick,
   orderbook, risk, and idempotency immediately before a paper/live order.
   Unknown or partial states pause new entries and reconcile; ambiguous orders
   are never blindly retried.
2. Expand risk calculations for asset/source/correlation exposure, stale data,
   unrealized loss, fees/slippage, and minimum order size. Add invariant and
   property-style tests.
3. Persist provider probes, cache/freshness/quota state, and use that cache in
   REST/MCP/dashboard health output. Emit structured redacted audit/metrics
   events for every rejection and control transition.

### 6. Deployment, PostgreSQL, CI, documentation, and audit

Files: `Dockerfile`, `docker-compose.yml`, migration/config tests, CI
workflows, runbooks, architecture/threat/backtest docs, and
`docs/FINAL_AUDIT.md`.

1. Separate web, scheduler, MCP, and executor services with non-root users,
   read-only filesystems/capabilities, resource limits, health checks, and a
   one-shot migration job. Keep executor-only secrets out of other services.
2. Verify SQLite and PostgreSQL URL/configuration paths and migration indexes;
   do not commit runtime databases or secret material.
3. Add CI for tests, Ruff, mypy, migrations, secret scans, and compose
   validation. Update integration instructions for Hermes, OpenClaw, Codex
   CLI, and Claude Code.
4. Run the full gate (`pytest`, Ruff, mypy, migration upgrade/downgrade,
   `git diff --check`, secret scan), then write the final audit with changes,
   tests, limitations, security assumptions, disabled features, and migration
   risks. Stop before enabling live execution if any gate is incomplete.

## Focused commit sequence

1. `feat: add shared application services and durable operator control`
2. `feat: connect cli and mcp to application services`
3. `feat: complete strict weather and crypto paper pipelines`
4. `feat: add temporal backtesting and persisted explanations`
5. `feat: isolate executor and harden risk and observability`
6. `ops: add service deployment ci and final audit`

Each commit must pass its focused tests and must not contain live credentials,
private keys, operator tokens, or real order calls.
