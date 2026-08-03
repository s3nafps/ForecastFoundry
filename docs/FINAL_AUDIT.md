# ForecastFoundry hardening audit

Date: 2026-08-03  
Scope: production-hardening milestone after the general-agent prototype.

## Delivered

- CLI, REST, MCP, scheduler, and executor now share `ApplicationServices` for
  domain routes, evidence, prediction explanations, backtests, portfolio state,
  reconciliation, provider health, and execution control.
- Pause/resume is backed by `execution_control_state` and `kill_switch_events`
  with revision, request ID, actor, reason, idempotency, and conflict checks.
- Operator credentials are PBKDF2-hashed, permission-scoped, expiring,
  revocable/rotatable, constant-time verified, and rate limited. Production
  REST uses `Authorization: Bearer`; MCP uses a process capability.
- Weather strict mode delegates to the existing detailed normalizer. The
  compatibility registry keeps unresolved legacy routes visibly ambiguous;
  application scans use strict mode.
- BTC/ETH public Coinbase/Binance/Kraken candles have source-specific parsing,
  freshness/quality gates, a short cache, deterministic bootstrap/Monte Carlo
  probabilities, and persisted contract/evidence/prediction references.
- Temporal backtesting rejects duplicate timestamps and future features and
  reports baseline comparison, Brier/log loss, calibration, sharpness, P&L,
  drawdown, turnover, fees/slippage, rejection counts, and seeded intervals.
- The executor is a separate process boundary with idempotent paper orders,
  immediate control/geoblock/risk rechecks, fake-adapter seams, and unknown
  status reconciliation that pauses new entries.
- Provider health probes are cached and persisted; metrics are exposed at
  `/metrics` and logs continue to redact configured secrets.
- Compose separates migration, web, scheduler, MCP, executor, and optional
  PostgreSQL services with non-root/read-only/capability/resource controls.

## Verification

The focused hardening tests, existing regression tests, Ruff, and mypy were
run during implementation. The final gate to run before release is:

```text
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy app
DATABASE_URL=sqlite+aiosqlite:///./data/ci.db alembic upgrade head
git diff --check
```

The CI workflow runs the same checks and a repository secret scan.

## Explicitly disabled / not claimed

- Real orders, wallet transfers, private-key access, and customer-fund custody
  are not enabled by default and are not exercised by tests.
- The optional Polymarket SDK remains behind startup policy, encrypted-keystore
  validation, geoblock, closed-only, risk, and executor gates; a live adapter
  is not configured in the community compose file.
- X, Reddit, and GitHub documents are feature-only provenance. They do not
  authorize a trade or override resolution evidence.
- The first crypto model is a deterministic baseline, not investment advice or
  a performance guarantee. Model promotion requires a chronological baseline
  comparison and calibration review.

## Security assumptions and residual risks

- Operators must supply a high-entropy token through a secret manager and
  rotate it before expiry. The test-only legacy JSON-token compatibility path
  is not enabled outside `APP_ENV=test`.
- SQLite is suitable for a single scheduled writer. PostgreSQL plus the
  optional `asyncpg` extra is the production multi-process target; migration
  and index behavior should be exercised against a real PostgreSQL instance
  before enabling concurrent schedulers.
- Provider availability, licensing, attribution, jurisdiction/geoblock, and
  Polymarket terms still require operator review. Stale or contradictory
  authoritative data must reject candidates.
- The executor deliberately does not retry ambiguous provider orders. An
  operator must reconcile and clear the durable control state after review.

## Migration / rollback notes

Revisions `0003` and `0004` add durable control, credential, and health tables;
they do not rename or delete existing weather tables. Back up the runtime
database, run `alembic upgrade head`, verify the control row, and only then
start scheduler/MCP/executor services. Rollback is supported through Alembic
but should be performed during a maintenance window because it removes audit
history tables introduced by these revisions.
