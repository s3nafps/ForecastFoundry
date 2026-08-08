# WeatherEdge Initial Milestone Implementation Plan

> **For AI workers:** Required sub-skill: use executing-plans to implement this plan in the current session. Track steps with the checkboxes below.

> **Status:** Completed on 2026-08-02. This file preserves the original TDD execution
> template; its unchecked boxes are historical and are not the current project tracker.
> See [`docs/FINAL_AUDIT.md`](../../FINAL_AUDIT.md) for current delivery and verification
> status.

**Goal:** Build a production-capable, paper-only service that turns active Polymarket daily maximum-temperature bucket events into persisted forecast probabilities, signal decisions, $5 paper positions, and optional Telegram alerts.

**Architecture:** A single FastAPI process hosts read-only pages/API and APScheduler polling jobs. Async adapters isolate Gamma, CLOB, Open-Meteo, and Telegram formats; a deterministic domain core normalizes rules, calculates member probabilities, evaluates executable-price edges, and applies paper-ledger constraints. SQLite stores every source snapshot and decision through SQLAlchemy/Alembic.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, SQLite/aiosqlite, Pydantic Settings, httpx, APScheduler, websockets, Jinja2, PyYAML, pytest, Ruff, mypy, Docker Compose.

---

## File map

- `pyproject.toml`: runtime/dev dependencies and tool configuration.
- `.env.example`, `.gitignore`: safe configuration example and ignored secrets/runtime state.
- `app/config.py`: validated settings and permanent real-trading startup guard.
- `app/logging.py`: stdlib JSON formatter with secret redaction.
- `app/database.py`: async engine/session lifecycle and transaction dependency.
- `app/models.py`: milestone SQLAlchemy persistence model.
- `app/schemas.py`: validated adapter/API/domain transfer objects.
- `app/services/http.py`: timeouts, retry/backoff, pacing, and circuit breaker.
- `app/services/polymarket.py`: Gamma discovery, CLOB books, and response normalization.
- `app/services/rules.py`: deterministic station/date/rounding/bucket normalization.
- `app/services/forecast.py`: `ForecastProvider` and Open-Meteo member adapter.
- `app/services/probability.py`: timezone aggregation, bias, rounding, and weighted bucket probabilities.
- `app/services/signals.py`: complete acceptance/rejection evaluation and alert fingerprinting.
- `app/services/paper.py`: Decimal paper-ledger sizing, entry, and manual settlement.
- `app/services/telegram.py`: outbound paper-only alert formatting/sending.
- `app/services/websocket.py`: optional public order-book stream and heartbeat.
- `app/worker.py`: one complete transactional scan flow and scheduler jobs.
- `app/api.py`: read-only `/api/v1` endpoints.
- `app/dashboard.py`, `app/templates/*.html`: escaped server-rendered views.
- `app/main.py`: application lifespan, safety gate, scheduler, routes, health/readiness.
- `config/stations.yaml`: audited station coordinates/timezones with source URLs.
- `config/market_overrides.yaml`: explicit per-market corrections, empty by default.
- `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`: initial schema.
- `tests/fixtures/*.json`: recorded Gamma, CLOB, and Open-Meteo examples.
- `tests/test_*.py`: unit, adapter, persistence, API, and end-to-end checks.
- `Dockerfile`, `docker-compose.yml`: non-root container and persistent internal SQLite volume.
- `docs/reverse-proxy.conf`, `README.md`: deployment and operations.

### Task 1: Project foundation and startup safety

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/logging.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing startup-safety tests**

```python
from decimal import Decimal
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_real_trading_can_never_be_enabled() -> None:
    with pytest.raises(ValidationError, match="permanently disabled"):
        Settings(real_trading_enabled=True)


def test_paper_balance_defaults_to_five_dollars() -> None:
    assert Settings().paper_starting_balance == Decimal("5.00")
```

- [ ] **Step 2: Run the tests and verify the expected import failure**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL because `app.config` does not exist.

- [ ] **Step 3: Add dependencies/tool configuration and minimum validated settings**

Implement a frozen `BaseSettings` model using `.env`, `SecretStr` for the Telegram token, `Decimal` for monetary thresholds, positive polling intervals, probability bounds from zero to one, and:

```python
@model_validator(mode="after")
def reject_real_trading(self) -> "Settings":
    if self.real_trading_enabled:
        raise ValueError("real trading is permanently disabled in WeatherEdge v1")
    return self
```

Configure Ruff for Python 3.12 with `E,F,I,UP,B,ASYNC`, mypy strict mode for `app`, and pytest asyncio auto mode. Use a stdlib `logging.Formatter` that emits JSON and replaces configured secrets before serialization.

- [ ] **Step 4: Install the project and run the focused checks**

Run: `python -m pip install -e ".[dev]"`
Run: `python -m pytest tests/test_config.py -q`
Run: `python -m ruff check app/config.py app/logging.py tests/test_config.py`
Run: `python -m mypy app/config.py app/logging.py`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .env.example .gitignore app tests/test_config.py
git commit -m "build: establish safe WeatherEdge configuration"
```

### Task 2: Async persistence and migration

**Files:**
- Create: `app/database.py`
- Create: `app/models.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/0001_initial.py`
- Test: `tests/test_database.py`

- [ ] **Step 1: Write failing persistence tests**

Create an isolated temporary SQLite database, migrate it to `head`, insert one `Event`, `Market`, `Outcome`, and `RejectedSignal`, commit, reopen a session, and assert exact IDs/rules/rejection JSON survive. Add a uniqueness check for `(market_id, token_id)` outcomes and a UTC-aware timestamp assertion.

- [ ] **Step 2: Verify the migration/persistence tests fail**

Run: `python -m pytest tests/test_database.py -q`
Expected: FAIL because database/models/migration are absent.

- [ ] **Step 3: Implement the schema from the approved design**

Use SQLAlchemy 2 `Mapped[...]` declarations and one `Base`. Store external numeric identifiers as strings. Use `JSON` for raw payloads, member series, probabilities, provenance, and rejection reasons. Use `Numeric` for prices/money, foreign-key cascades only for owned child records, and explicit indexes for `(market_id, captured_at)`, `(provider, retrieved_at)`, open paper positions, and signal fingerprints.

Create all approved tables, including the observations table as persistence only; do not create an observation provider.

- [ ] **Step 4: Run migration and persistence checks**

Run: `$env:DATABASE_URL='sqlite+aiosqlite:///./test-migration.db'; python -m alembic upgrade head`
Run: `python -m pytest tests/test_database.py -q`
Run: `python -m ruff check app/database.py app/models.py alembic tests/test_database.py`
Run: `python -m mypy app/database.py app/models.py`
Expected: all commands exit 0 and migration creates every approved table.

- [ ] **Step 5: Commit**

```bash
git add app/database.py app/models.py alembic.ini alembic tests/test_database.py
git commit -m "feat: add WeatherEdge persistence schema"
```

### Task 3: Deterministic temperature domain core

**Files:**
- Create: `app/schemas.py`
- Create: `app/services/__init__.py`
- Create: `app/services/rules.py`
- Create: `app/services/probability.py`
- Create: `config/stations.yaml`
- Create: `config/market_overrides.yaml`
- Test: `tests/test_rules.py`
- Test: `tests/test_probability.py`

- [ ] **Step 1: Write failing rule and probability tests**

Cover Celsius/Fahrenheit conversion; half-up, floor, and ceiling rounding; exact/lower/upper bucket labels; rejection of overlaps/gaps; London local-day boundaries; daily maximum; zero/default bias; equal model weighting; excluded missing members; ensemble spread; and ambiguity rejection when station, date, timezone, coordinates, or rounding is missing.

The recorded London rule test must assert station `EGLC`, timezone `Europe/London`, local date `2026-08-02`, unit `celsius`, measurement `daily_max_temperature`, whole-degree precision, and bucket labels from all sibling markets.

- [ ] **Step 2: Verify tests fail for missing modules**

Run: `python -m pytest tests/test_rules.py tests/test_probability.py -q`
Expected: FAIL because the domain functions do not exist.

- [ ] **Step 3: Implement minimal immutable domain schemas and functions**

Expose these stable functions:

```python
def fahrenheit_to_celsius(value: float) -> float: ...
def celsius_to_fahrenheit(value: float) -> float: ...
def round_temperature(value: float, method: RoundingMethod, precision: int = 0) -> float: ...
def parse_bucket(label: str, unit: TemperatureUnit) -> Bucket: ...
def normalize_temperature_event(
    event: GammaEvent, stations: StationRegistry, override: dict[str, object] | None = None
) -> NormalizedEvent: ...
def daily_maximum(
    points: Sequence[ForecastPoint], local_date: date, timezone: str
) -> float | None: ...
def calculate_probabilities(
    members: Sequence[MemberDailyValue],
    buckets: Sequence[Bucket],
    model_weights: Mapping[str, float],
) -> ProbabilityResult: ...
```

Use `zoneinfo.ZoneInfo`, `Decimal.quantize` for explicit half-up rounding, fixed confidence points, and complete bucket coverage checks. Load YAML with `yaml.safe_load`; reject unknown override keys with Pydantic validation. Seed only EGLC with an audited source URL so the recorded flow is supported without invented coordinates.

- [ ] **Step 4: Run the domain checks**

Run: `python -m pytest tests/test_rules.py tests/test_probability.py -q`
Run: `python -m ruff check app/schemas.py app/services/rules.py app/services/probability.py tests/test_rules.py tests/test_probability.py`
Run: `python -m mypy app/schemas.py app/services/rules.py app/services/probability.py`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add app/schemas.py app/services config tests/test_rules.py tests/test_probability.py
git commit -m "feat: normalize temperature markets and probabilities"
```

### Task 4: Resilient public data adapters

**Files:**
- Create: `app/services/http.py`
- Create: `app/services/polymarket.py`
- Create: `app/services/forecast.py`
- Create: `app/services/websocket.py`
- Create: `tests/fixtures/london_event.json`
- Create: `tests/fixtures/london_books.json`
- Create: `tests/fixtures/london_ensemble.json`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Record sanitized public response fixtures and write failing adapter tests**

Assert Gamma search expands one event into sibling binary markets and decodes JSON-string `outcomes`/`clobTokenIds`. Assert CLOB asks are sorted by lowest executable price regardless of response order, depth is summed at/under the configured slippage price, and an empty ask list yields no executable price. Assert Open-Meteo dynamically discovers `temperature_2m_memberNN` arrays, preserves model/member identity, validates aligned timestamps, and skips only unavailable models.

Test retryable 429/5xx behavior, non-retryable validation failures, circuit opening after the configured threshold, and secret-free structured error details.

- [ ] **Step 2: Verify adapter tests fail**

Run: `python -m pytest tests/test_providers.py -q`
Expected: FAIL because provider adapters are absent.

- [ ] **Step 3: Implement the shared HTTP policy and public adapters**

Use one injected `httpx.AsyncClient`. Retry only timeouts, connection errors, 429, and 5xx responses, bounded by `HTTP_MAX_RETRIES`; honor `Retry-After` when valid and otherwise use exponential backoff plus jitter. A provider-local circuit breaker uses monotonic time and one `asyncio.Lock`-protected state record.

Define and implement:

```python
class ForecastProvider(Protocol):
    async def get_forecast(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        timezone: str,
    ) -> ForecastResult: ...
```

Polling remains complete without WebSocket. The listener only accepts public market events, sends text `PING` every ten seconds, and publishes validated books through an injected callback.

- [ ] **Step 4: Run adapter checks**

Run: `python -m pytest tests/test_providers.py -q`
Run: `python -m ruff check app/services/http.py app/services/polymarket.py app/services/forecast.py app/services/websocket.py tests/test_providers.py`
Run: `python -m mypy app/services/http.py app/services/polymarket.py app/services/forecast.py app/services/websocket.py`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add app/services tests/fixtures tests/test_providers.py
git commit -m "feat: add resilient public market and forecast adapters"
```

### Task 5: Signal decisions and $5 paper ledger

**Files:**
- Create: `app/services/signals.py`
- Create: `app/services/paper.py`
- Test: `tests/test_signals.py`
- Test: `tests/test_paper.py`

- [ ] **Step 1: Write failing decision and ledger tests**

Test the exact usable-edge formula, every configured rejection predicate, complete multi-reason collection, duplicate fingerprint cooldown/material-change behavior, estimated fee calculation, minimum-share rejection, insufficient-balance rejection, no overlapping open entry, exact cash deduction, and manual settlement P&L.

Include the boundary where five shares at an ask below $1 fit in $5 before fees but are rejected after fees/slippage exceed the remaining cash.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_signals.py tests/test_paper.py -q`
Expected: FAIL because signal and paper modules are absent.

- [ ] **Step 3: Implement pure decisions and transactional ledger operations**

Expose:

```python
def calculate_usable_edge(candidate: SignalCandidate, buffers: EdgeBuffers) -> EdgeResult: ...
def evaluate_signal(candidate: SignalCandidate, policy: SignalPolicy) -> SignalDecision: ...
def alert_fingerprint(decision: AcceptedSignal, policy: DedupePolicy) -> str: ...
def estimate_entry(
    ask: Decimal, shares: Decimal, fee_rate: Decimal, slippage: Decimal
) -> EntryQuote: ...
async def open_paper_position(
    session: AsyncSession, signal: Signal, quote: EntryQuote
) -> PaperPosition: ...
async def settle_paper_position(
    session: AsyncSession, position_id: int, won: bool
) -> PaperSettlement: ...
```

Pure evaluation never writes. Ledger functions lock logically through the SQLite write transaction, re-read available cash/open positions, and either atomically insert or raise a typed rejection.

- [ ] **Step 4: Run decision and ledger checks**

Run: `python -m pytest tests/test_signals.py tests/test_paper.py -q`
Run: `python -m ruff check app/services/signals.py app/services/paper.py tests/test_signals.py tests/test_paper.py`
Run: `python -m mypy app/services/signals.py app/services/paper.py`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add app/services/signals.py app/services/paper.py tests/test_signals.py tests/test_paper.py
git commit -m "feat: evaluate paper signals and enforce ledger limits"
```

### Task 6: End-to-end scanner and Telegram alerts

**Files:**
- Create: `app/services/telegram.py`
- Create: `app/worker.py`
- Test: `tests/test_telegram.py`
- Test: `tests/test_end_to_end.py`

- [ ] **Step 1: Write failing alert and recorded-flow tests**

Assert the alert includes PAPER ONLY, market/outcome, probability, executable ask, raw/usable edge, model counts, station, horizon, spread, and rule confidence. Assert HTML is escaped or plain text is used safely.

The end-to-end test injects recorded London Gamma/CLOB/Open-Meteo adapters, runs one scan against migrated SQLite, and asserts events/markets/books/rules/runs/members/probabilities plus exactly one accepted signal or explicit rejected-signal row per candidate. Configure thresholds so one fixture outcome passes, assert one paper position, and assert one mocked Telegram request. Re-run unchanged and assert no duplicate position/alert.

- [ ] **Step 2: Verify the flow tests fail**

Run: `python -m pytest tests/test_telegram.py tests/test_end_to_end.py -q`
Expected: FAIL because worker and Telegram modules are absent.

- [ ] **Step 3: Implement one transactional scanner flow**

`scan_once` accepts adapters, settings, session factory, and clock. Each event is isolated so one malformed event records a provider/data error and scanning continues. Persist source snapshots before derived calculations. Commit accepted/rejected decisions together with their exact input identifiers. Send Telegram only after the database commit; a send failure updates signal alert state and records a provider error without rolling back the paper ledger.

- [ ] **Step 4: Run flow checks**

Run: `python -m pytest tests/test_telegram.py tests/test_end_to_end.py -q`
Run: `python -m ruff check app/services/telegram.py app/worker.py tests/test_telegram.py tests/test_end_to_end.py`
Run: `python -m mypy app/services/telegram.py app/worker.py`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add app/services/telegram.py app/worker.py tests/test_telegram.py tests/test_end_to_end.py
git commit -m "feat: run the complete paper-only weather scan"
```

### Task 7: FastAPI, dashboard, scheduler, and health

**Files:**
- Create: `app/api.py`
- Create: `app/dashboard.py`
- Create: `app/main.py`
- Create: `app/templates/base.html`
- Create: `app/templates/overview.html`
- Create: `app/templates/market.html`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write failing API/lifespan tests**

Test `/health` returns immediately, `/ready` verifies a database query, `/api/v1/markets`, `/api/v1/signals`, `/api/v1/positions`, `/api/v1/errors`, and `/api/v1/config` return validated read-only data, and dashboard output escapes rule/question HTML. Assert configuration output redacts Telegram token and database credentials. Assert paused state prevents scheduled scans while health/read APIs remain available.

- [ ] **Step 2: Verify API tests fail**

Run: `python -m pytest tests/test_api.py -q`
Expected: FAIL because the FastAPI application is absent.

- [ ] **Step 3: Implement application lifespan and read-only presentation**

Create the shared client/adapters/scheduler in FastAPI lifespan. Schedule the scan with `max_instances=1`, `coalesce=True`, and configured seconds. Start the optional WebSocket only when enabled. Shutdown scheduler, listener, HTTP client, and engine in reverse order.

Keep routes read-only except an internal/manual scan hook enabled only under the test environment. Use Jinja autoescape and no `safe` filter for external text.

- [ ] **Step 4: Run web checks**

Run: `python -m pytest tests/test_api.py -q`
Run: `python -m ruff check app/api.py app/dashboard.py app/main.py tests/test_api.py`
Run: `python -m mypy app/api.py app/dashboard.py app/main.py`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add app/api.py app/dashboard.py app/main.py app/templates tests/test_api.py
git commit -m "feat: expose WeatherEdge dashboard and read-only API"
```

### Task 8: Container deployment and operator documentation

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `docs/reverse-proxy.conf`
- Create: `README.md`

- [ ] **Step 1: Add deployment smoke expectations**

Document the executable verification commands before writing deployment files: Compose must expose only the HTTP service port, mount `/data` as a named volume, run `alembic upgrade head` before Uvicorn, use an unprivileged user, define `/health` health checking, and restart unless stopped.

- [ ] **Step 2: Implement the minimal production container**

Use `python:3.12-slim`, install the project wheel, create a non-root `weatheredge` user, create owned `/data`, and run migration plus `uvicorn app.main:app --host 0.0.0.0 --port 8000`. Do not install compilers or copy `.env` into the image.

- [ ] **Step 3: Write complete README operations**

Include paper-only warning, architecture/data sources, local install, configuration, Telegram BotFather/chat ID setup, migrations, tests, Docker/VPS deployment, optional Nginx TLS proxy, named-volume backup/restore, logs, upgrades, troubleshooting, known station/rule/model limitations, and the statement that forecast probability is not truth.

- [ ] **Step 4: Verify deployment artifacts**

Run: `docker compose config`
Run: `docker build -t weatheredge:test .`
Run: `docker compose up -d --build`
Run: `docker compose ps`
Run: `Invoke-RestMethod http://localhost:8000/health`
Run: `Invoke-RestMethod http://localhost:8000/ready`
Run: `docker compose down`
Expected: config/build/start succeed, service is healthy, both endpoints return success, and the persistent volume is retained.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore docs/reverse-proxy.conf README.md
git commit -m "docs: add production container and VPS operations"
```

### Task 9: Final verification and acceptance audit

**Files:**
- Modify only files implicated by failures.

- [ ] **Step 1: Run the complete local quality gate**

Run: `python -m pytest -q`
Run: `python -m ruff check .`
Run: `python -m ruff format --check .`
Run: `python -m mypy app`
Expected: every command exits 0 with no warnings treated as failures.

- [ ] **Step 2: Verify a fresh database and safe startup**

Run: `$env:DATABASE_URL='sqlite+aiosqlite:///./acceptance.db'; python -m alembic upgrade head`
Run: `$env:REAL_TRADING_ENABLED='true'; python -c "from app.config import Settings; Settings()"`
Expected: migration exits 0; unsafe configuration exits nonzero and names the permanent safety rule.

- [ ] **Step 3: Audit prohibited capabilities and placeholders**

Run: `rg -n -i "private.?key|seed phrase|sign.*order|post.*order|cancel.*order|wallet|TODO|TBD|NotImplemented" app tests README.md`
Expected: no real-trading implementation or unfinished placeholder; documentation-only warnings are reviewed manually.

- [ ] **Step 4: Audit each acceptance criterion against evidence**

Record in the final handoff: Docker health evidence; discovered fixture/live-format support; ambiguous rejection test; generated distribution test; exact bucket mapping test; executable ask test; accepted/rejected persistence test; Telegram mock test; paper balance test; pytest/Ruff/mypy outputs; no-wallet scan; README deployment sections.

- [ ] **Step 5: Commit any verification fixes**

```bash
git add -A
git commit -m "test: complete WeatherEdge acceptance verification"
```
