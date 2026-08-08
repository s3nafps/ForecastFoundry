# ForecastFoundry hardening audit

Date: 2026-08-08
Scope: completion of the general-agent milestone with observation-informed
probabilities, calibration-based model weighting, research evidence, and the
final gate.

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

The final completion gate ran on 2026-08-06:

```text
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app
DATABASE_URL=sqlite+aiosqlite:///./acceptance.db alembic upgrade head
DATABASE_URL=sqlite+aiosqlite:///./acceptance.db alembic downgrade base
git diff --check
```

Results: 252 tests passed, 1 skipped (PostgreSQL integration skipped locally),
Ruff check and format clean, mypy clean, Alembic upgrade to revision `0010` and
downgrade to base verified against SQLite, and `git diff --check` clean.
Migrations span revisions `0001` through `0010`.

Completed features in this gate:

- Observation-informed probabilities: AviationWeather METAR ingestion with the
  `observation_blend_hours`/`observation_min_count` gate, observation-guided
  member reconciliation, and `observations_used`/`blend_applied` on estimates.
- Calibration-based model weighting: `forecastfoundry calibrate` with the
  30-sample / 5% improvement promotion gate and equal-weight default, persisted
  under `model_weights:weather`.
- Research evidence: GitHub issue ingestion into `research_documents` with
  feature-only provenance, `GET /api/v1/research`, and the `/research`
  dashboard page.
- CI: three jobs — `quality` (pytest, Ruff, mypy, SQLite migrations including
  downgrade, `git diff --check`, repository secret material scan), `postgres`
  (postgres:16 service, Alembic up/down/up and
  `tests/test_postgres_integration.py` via `FORECASTFOUNDRY_TEST_POSTGRES=1`,
  with the `asyncpg` dev extra), and `docker` (build, compose up, `/health` and
  `/ready` polls, all-services-healthy gate, compose down).

CI run URLs (quality, PostgreSQL, and Docker jobs green):
- main run: https://github.com/s3nafps/ForecastFoundry/actions/runs/31203577652
- review-fixes run: https://github.com/s3nafps/ForecastFoundry/actions/runs/31205008760

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
they do not rename or delete existing weather tables. Revisions `0005`-`0010`
add execution-control idempotency, crypto evidence signals, domain contract
identity, paper lifecycle state, and observation-blend columns
(`observations_used`, `blend_applied`) on probability estimates. Back up the
runtime database, run `alembic upgrade head`, verify the control row, and only
then start scheduler/MCP/executor services. Rollback is supported through
Alembic but should be performed during a maintenance window because it removes
audit history tables introduced by these revisions.
