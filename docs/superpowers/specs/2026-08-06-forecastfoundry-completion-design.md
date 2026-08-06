# ForecastFoundry Completion Design

## Scope

Complete the ForecastFoundry general-agent branch and land it on `master` as the finished product. The branch is one failing test away from a clean gate and carries uncommitted in-flight work. Completion covers four areas:

1. Close the loop on the in-flight settlement work and the failing test.
2. Observation-informed weather probabilities (previously deferred).
3. Calibration-based model weighting (previously deferred).
4. Research evidence ingestion, GitHub first (previously deferred).
5. Deployment verification through CI, because Docker is not installed in the local build environment.
6. Squash-merge the branch into `master` and refresh release documentation.

Decision points approved on 2026-08-06:

- Observation model: observation-guided member reconciliation (deterministic; no invented priors).
- Calibration promotion gate: at least 30 settled samples per model and walk-forward Brier at least 5% better than the equal-weight baseline; otherwise equal weights.
- Research scope: GitHub public issue search only in this milestone; X and Reddit are documented future work with no placeholder code.

## Current state (verified 2026-08-06)

- `master` holds the completed weather-only v1 milestone (9 task commits, acceptance gate green at commit time).
- `feature/forecastfoundry-general-agent` (worktree `.worktrees/weatheredge-general-agent`) is 38 commits ahead of master with 216 passing tests, clean Ruff, clean mypy, and one failing test: `tests/test_weather_settlement_strict.py`.
- The failing test is a bucket-label canonicalization mismatch: settlement evidence carries the mojibake label `24Â°C or higher` while the weather contract stores `24°C or higher` (real degree sign from the recorded fixture).
- Uncommitted WIP: `app/domains/crypto.py` (price-definition canonicalization), `app/services/paper.py` (strict weather/crypto settlement invariants), `tests/test_crypto_contracts.py` (two new tests), and the untracked `tests/test_weather_settlement_strict.py`.
- The `observations` table exists in the schema and settlement reads it, but no code writes to it. `observation_poll_seconds` exists in settings and the scheduler runs weather scan, crypto scan, and settlement jobs; there is no observation ingestion job.
- `app/services/calibration.py` already provides `brier_score` and `walk_forward_calibration`; no per-model probability snapshot is persisted at signal time, so per-model calibration is not yet possible.
- `app/providers/research.py` contains only `sanitize_research_text`; no ingestion, persistence, or API surface.
- CI (`.github/workflows/ci.yml`) runs pytest, Ruff, mypy, SQLite migration, diff check, and a secret scan. No Docker job, no PostgreSQL job.
- Docker and PostgreSQL are not installed locally; verification of containers and PostgreSQL must run in CI.

## Phase 0: Close the loop

### 0.1 Bucket-label canonicalization

Introduce one shared helper `canonical_bucket_label(label: str) -> str` that normalizes the label for equality comparison: strips the `Â` mojibake prefix, trims whitespace, and collapses internal whitespace. Use it consistently:

- `app/services/rules.py` `parse_bucket` (replaces the local `label.replace("Â", "")` cleanup) so contract labels are canonical.
- `app/services/paper.py` weather settlement comparison: compare `canonical_bucket_label(normalized.get("bucket_label"))` with `canonical_bucket_label(bucket.label)` instead of raw string equality.
- The AviationWeather settlement normalizer emits canonical bucket labels.

The helper lives in a shared module (e.g. `app/services/rules.py` next to bucket parsing) and is covered by unit tests including the exact `24Â°C or higher` vs `24°C or higher` case.

### 0.2 Keep the crypto price-definition WIP

`app/domains/crypto.py` now rejects unsupported price definitions (`last price`, `index level`, `spot price`, `market price`, `reference price`) instead of silently accepting them, and canonicalizes the `close` alias to `closing price`. Keep these changes and their tests; they harden the contract boundary in the same spirit as the weather fix.

### 0.3 Gate and commit

- `pytest -q` green (217 tests).
- `ruff check .`, `mypy app` clean.
- Commit WIP as one Conventional Commit, e.g. `fix: canonicalize settlement bucket labels and price definitions`.

## Phase 1: Observation-informed probabilities (weather)

### 1.1 Observation ingestion adapter

New module `app/services/observations.py`:

- Adapter `AviationWeatherObservations` fetches hourly METAR observations for a station and date from `https://aviationweather.gov/api/data/metar?ids=<ICAO>&format=json&date=<yyyymmdd>` through the shared resilient HTTP client. The endpoint shape matches what the strict settlement path already consumes (`icaoId`, `obsTime` epoch seconds, `temp` Celsius, `rawOb`, `receiptTime`).
- A pure parser `parse_aviation_weather_observations(payload, station_id)` validates rows, converts `obsTime` to UTC-aware datetimes, applies quality flags, and returns typed `ObservedHour` values. Fatal flags (`fatal`, `invalid`, `missing_temperature`) drop rows.
- A persistence helper `ingest_observations(session, station_id, source, rows)` upserts into the existing `observations` table, storing the AviationWeather raw fields under `raw_data`; `source` is set to the settlement contract's authoritative `resolution_source` URL.
- Scheduler integration: a new `scheduled_observation_ingest` job runs at `observation_poll_seconds` (setting already exists). It ingests only for active weather contracts that are within the observation blend horizon of resolution, using their normalized `station_id` and `local_date`. Errors record `ProviderError` rows and never abort other jobs.

### 1.2 Observation-guided member reconciliation

In `scan_once` (weather path), when `expiry - now <= observation_blend_hours` and observations exist for the station and local date:

1. Load ingested observations for the station/day (ordered by `observed_at`).
2. For each ensemble member, replace every hourly `ForecastPoint` with timestamp `<= now` by the nearest observation's temperature. Points after `now` stay untouched.
3. Recompute each member's local-calendar `daily_maximum`, then `calculate_probabilities` with the existing bucket/rounding path.

This is deterministic and uses only real data: a member whose observed-to-now path already crossed a bucket ceiling cannot map back down, so its probability mass moves deterministically upward. No new forecast model is invented.

### 1.3 Quality gates

- If the blend horizon applies but observations are missing, stale, or below `observation_min_count`, the candidate is rejected with `observations_stale` (the predicate already exists in `app/services/signals.py`) and a `RejectedSignal` row is persisted.
- `observations_required` becomes true when inside the blend horizon.

### 1.4 Settings and schema

New settings (with defaults):

- `observation_blend_hours: int = 36`
- `observation_min_count: int = 6` (matches the settlement coverage floor)

Migration `0009_observation_blend.py` adds to `probability_estimates`:

- `observations_used: int` default 0
- `blend_applied: bool` default false

Migration `0010_research_documents.py` creates the `research_documents` table (see Phase 3).

The alert message includes the observation summary instead of the placeholder `"not collected in temperature milestone"`.

### 1.5 Tests

- Unit: point-replacement before `now` only; member censoring when observed path exceeds a bucket ceiling; probability shift when the blend applies.
- Adapter: parse recorded AviationWeather JSON fixture (recorded, sanitized); reject fatal-flag rows; misaligned/missing rows raise typed errors.
- Integration: one e2e scan with fixture observations inside the blend horizon produces changed `probability_estimates` with `blend_applied=true` and `observations_used>0`.
- Rejection: inside blend horizon with no observations -> `observations_stale` rejection persisted.
- Persistence: migration 0009 upgrades/downgrades on a fresh SQLite database.

## Phase 2: Calibration-based model weighting

### 2.1 Per-model probability snapshot

At signal time, `scan_once` computes the outcome probability per model (calling `calculate_probabilities` per model over that model's valid members) and stores `signal_data["model_probabilities"] = {model: probability}` on the `Signal` row. The aggregate probability and decision logic are unchanged.

### 2.2 Weight computation and promotion

New module `app/services/model_weights.py`:

- `per_model_brier(settlements, signals)`: for each settled position, join `PaperSettlement -> PaperPosition -> Signal` and score each model with `(model_probability - outcome) ** 2` using `signal_data["model_probabilities"]` (missing model entries are ignored; a settlement without the snapshot contributes nothing).
- `compute_weights(per_model_brier, *, min_samples=30, min_improvement=Decimal("0.05")) -> dict[str, float] | None`: inverse-Brier normalized weights, promoted only when every candidate model has at least `min_samples` and the walk-forward Brier of the weighted blend is at least `min_improvement` better than the equal-weight baseline. Returns `None` (equal weights) otherwise.
- `load_weights(session)` / `store_weights(session, weights)`: `application_settings` key `model_weights:weather` (JSON of model -> weight), versioned by `updated_at`.

Promotion runs on demand via CLI (`ff calibrate`) and is invoked by the settlement job after new settlements; it never runs inside the scan path.

`scan_once` reads persisted weights instead of passing `model_weights={}`; the equal-weight default is preserved when no promoted weights exist.

### 2.3 Tests

- Brier aggregation across models from synthetic settled signals.
- Gate: below-minimum samples -> `None`; improvement below 5% -> `None`; qualifying history -> promoted weights with correct normalization.
- Persistence round-trip and reload default.
- End-to-end: a synthetic settled history followed by `ff calibrate` changes the weights used by the next scan.

## Phase 3: Research evidence (GitHub first)

### 3.1 Ingestion

New module `app/services/research.py`:

- `fetch_github_issues(http, repo, *, since)`: public issue search via the shared resilient client, throttled and cached (short TTL).
- Sanitize with the existing `sanitize_research_text`; hash the raw response with `canonical_payload_hash`.
- Persist to a new `research_documents` table (migration 0010):

  - `id`, `provider` (`github`), `external_id` (issue number), `title`, `body_snippet`, `url`, `author`, `published_at`, `raw_response_hash`, `sanitized_at`, `quality_flags`, `raw_data`.

- Evidence classification is `operational_research` (class 3) and is recorded in the provider registry; research documents can generate an investigation record or provider-health note but cannot affect rules, probabilities, or orders.

### 3.2 API and dashboard

- Read-only `GET /api/v1/research` returning recent documents.
- Dashboard page listing research documents.

### 3.3 Isolation guarantee

An architectural test proves the research path is inert: after ingestion, scanning a market with identical evidence produces identical probabilities regardless of research documents present.

### 3.4 Tests

- Sanitizer/hash unit tests.
- GitHub fixture ingestion (recorded response), rate-limit behavior through the resilient client.
- Persistence round-trip.
- Isolation test (3.3).

## Phase 4: Deployment verification in CI

Extend `.github/workflows/ci.yml` with two jobs (the existing `quality` job stays):

1. `docker` job: `docker build -t forecastfoundry:test .`; `docker compose config`; `docker compose up -d`; poll `/health` and `/ready`; `docker compose down`; assert the named volume is retained.
2. `postgres` job: `postgres:16` service container; `DATABASE_URL=postgresql+asyncpg://...` `alembic upgrade head` and `alembic downgrade base`; run the full pytest suite against PostgreSQL.

`asyncpg` is added as a dev/test extra in `pyproject.toml` (the runtime extra stays optional). The compose file already separates migration, web, scheduler, MCP, executor, and optional PostgreSQL services; CI exercises the SQLite compose profile for the Docker job and the PostgreSQL URL for the migration/test job.

## Phase 5: Merge and release

1. Re-run the full local gate in the worktree: pytest, Ruff, mypy, fresh-database migration (up and down), `git diff --check`.
2. Verify CI green on the pushed branch (all three jobs).
3. Squash-merge `feature/forecastfoundry-general-agent` into `master` with one Conventional Commit: `feat: complete ForecastFoundry with observations, calibration, and research evidence`.
4. Update `master` README to the ForecastFoundry product description, add the observation/calibration/research sections, and refresh `docs/FINAL_AUDIT.md` with CI evidence.
5. Optionally tag `v0.2.0`.

## Verification strategy

Every phase ends with the same gate:

- Unit tests for all new pure functions.
- Adapter tests on recorded fixtures (no live network in tests).
- At least one integration/e2e test per feature.
- Full local gate: `pytest -q`, `ruff check .`, `mypy app`, fresh `alembic upgrade head` and downgrade, `git diff --check`.
- CI green (quality, docker, postgres jobs) before the merge.

Completion evidence: the squash commit hash, the CI run URLs for all three jobs, and the updated FINAL_AUDIT.

## Out of scope

- X and Reddit research ingestion (requires user credentials; documented as future work, no placeholder code).
- Additional domains (sports, elections, rainfall, snow, hurricanes).
- Live (non-paper) execution enablement; the execution stack remains behind the existing gates and is not activated by this work.
- Telegram command polling.
- Installing Docker locally.
