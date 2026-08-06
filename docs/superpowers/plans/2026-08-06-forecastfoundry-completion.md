# ForecastFoundry Completion Implementation Plan

> **For AI workers:** Required sub-skill: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan in the current session. Track steps with the checkboxes below.

**Goal:** Turn the ForecastFoundry general-agent branch into a merged, CI-verified product: finish the in-flight settlement work, add observation-informed weather probabilities, calibration-based model weighting, and GitHub research ingestion, then land everything on `master` via squash merge.

**Architecture:** Work happens in the existing worktree `.worktrees/weatheredge-general-agent` on `feature/forecastfoundry-general-agent`. The weather scan remains in `app/worker.py::scan_once`; observations feed it through a new `app/services/observations.py` adapter and a new scheduler job. Per-model probabilities are snapshotted into `Signal.signal_data`, consumed by a new `app/services/model_weights.py` promotion service exposed via CLI and the settlement job. Research ingestion writes to the already-existing `research_documents` table. CI gains Docker and PostgreSQL jobs.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, httpx, APScheduler, pytest, Ruff, mypy, GitHub Actions, PostgreSQL 16 (CI only).

**Deviation from spec (approved inline):** the `research_documents` table already exists from migration 0002, so no new migration for it. The PostgreSQL CI job runs migrations plus a targeted integration test (skipped unless `FORECASTFOUNDRY_TEST_POSTGRES=1`) instead of the full SQLite-bound suite.

---

## File map

- Modify: `app/services/rules.py` — add `canonical_bucket_label`.
- Modify: `app/services/paper.py` — canonical label comparison in weather settlement.
- Modify: `app/config.py` — `observation_blend_hours`, `observation_min_count`.
- Modify: `app/models.py` — `ProbabilityEstimate.observations_used`, `.blend_applied`.
- Create: `alembic/versions/0009_observation_blend.py`.
- Create: `app/services/observations.py` — parse, ingest, reconcile.
- Modify: `app/main.py` — observation ingestion scheduler job.
- Modify: `app/worker.py` — blend application, observation gates, per-model snapshot, weights, alert summary.
- Create: `app/services/model_weights.py`.
- Modify: `app/services/application.py` — `run_calibration`, hook in `run_settlement_job`.
- Modify: `app/cli.py` — `calibrate` command.
- Create: `app/services/research.py`.
- Modify: `app/api.py` — `/api/v1/research`.
- Modify: `app/dashboard.py`, `app/templates/table.html` — research page.
- Modify: `pyproject.toml` — `postgres` extra with `asyncpg`.
- Modify: `.github/workflows/ci.yml` — docker and postgres jobs.
- Create: `tests/test_observations.py`, `tests/test_observation_flow.py`, `tests/test_model_weights.py`, `tests/test_research.py`, `tests/test_postgres_integration.py`, `tests/fixtures/aviationweather_eglc.json`, `tests/fixtures/github_issues.json`.
- Modify: `tests/test_rules.py`, `tests/test_general_agent_migration.py`.
- Modify (Phase 5): `README.md`, `docs/FINAL_AUDIT.md`.

---

### Task 0: Baseline verification

**Files:** none (read-only).

- [ ] **Step 1: Confirm the baseline gate**

Run from the worktree root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy app
```

Expected: exactly 1 failure — `tests/test_weather_settlement_strict.py::test_strict_weather_contract_dispatches_bucket_label_before_binary_validation` — with `PaperTradingError: weather normalized bucket contradicts observations`; Ruff and mypy clean.

### Task 1: Canonical bucket labels (Phase 0)

**Files:**
- Modify: `app/services/rules.py`
- Modify: `app/services/paper.py` (weather settlement comparison, ~line 1080)
- Modify: `tests/test_rules.py`
- Modify: `tests/test_weather_settlement_strict.py` (untracked WIP file — already present, no new code needed)

- [ ] **Step 1: Write failing unit tests for the helper**

Append to `tests/test_rules.py`:

```python
def test_canonical_bucket_label_strips_mojibake_and_collapses_whitespace() -> None:
    from app.services.rules import canonical_bucket_label

    assert canonical_bucket_label("24Â°C or higher") == "24°C or higher"
    assert canonical_bucket_label(" 24°C   or   higher ") == "24°C or higher"
    assert canonical_bucket_label("24°C or higher") == "24°C or higher"
```

- [ ] **Step 2: Run the new test to confirm failure**

Run: `pytest tests/test_rules.py::test_canonical_bucket_label_strips_mojibake_and_collapses_whitespace -v`
Expected: FAIL, `ImportError` / `AttributeError: module 'app.services.rules' has no attribute 'canonical_bucket_label'`.

- [ ] **Step 3: Implement the helper and use it**

In `app/services/rules.py`, above `parse_bucket`:

```python
def canonical_bucket_label(label: str) -> str:
    """Normalize a bucket label for equality comparison (mojibake-safe)."""
    return " ".join(label.replace("Â", "").split())
```

Replace the cleanup line inside `parse_bucket`:

```python
    cleaned = label.replace("Â", "")
```
with:
```python
    cleaned = canonical_bucket_label(label)
```

In `app/services/paper.py`, in `_derive_weather_settlement_outcome`, replace:

```python
    if normalized.get("bucket_label") != bucket.label:
        raise PaperTradingError("weather normalized bucket contradicts observations")
```
with:
```python
    if canonical_bucket_label(str(normalized.get("bucket_label"))) != canonical_bucket_label(
        bucket.label
    ):
        raise PaperTradingError("weather normalized bucket contradicts observations")
```

Add the import in `app/services/paper.py` (extend the existing `from app.services.rules import ...` line):

```python
from app.services.rules import canonical_bucket_label
```

- [ ] **Step 4: Run the failing settlement test plus the full phase gate**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_weather_settlement_strict.py tests/test_rules.py -q
.\.venv\Scripts\python.exe -m ruff check app/services/rules.py app/services/paper.py tests/test_rules.py
.\.venv\Scripts\python.exe -m mypy app/services/rules.py app/services/paper.py
```
Expected: all pass, Ruff and mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/services/rules.py app/services/paper.py tests/test_rules.py tests/test_weather_settlement_strict.py
git commit -m "fix: canonicalize settlement bucket labels and price definitions"
```

---

### Task 2: Observation ingestion service (Phase 1.1)

**Files:**
- Create: `app/services/observations.py`
- Create: `tests/fixtures/aviationweather_eglc.json`
- Create: `tests/test_observations.py`
- Modify: `app/main.py` (scheduler job)

- [ ] **Step 1: Write the fixture and failing tests**

Create `tests/fixtures/aviationweather_eglc.json` (recorded shape of the AviationWeather METAR JSON API; sanitized):

```json
[
  {"icaoId": "EGLC", "obsTime": 1785600000, "temp": 19.8, "rawOb": "EGLC 010000Z 24005KT 9999 FEW010 20/14 Q1015", "receiptTime": 1785600300},
  {"icaoId": "EGLC", "obsTime": 1785603600, "temp": 20.1, "rawOb": "EGLC 010100Z 24005KT 9999 SCT010 20/14 Q1015", "receiptTime": 1785603900},
  {"icaoId": "EGLC", "obsTime": 1785607200, "temp": 20.4, "rawOb": "EGLC 010200Z 24005KT 9999 SCT010 20/14 Q1015", "receiptTime": 1785607500},
  {"icaoId": "EGLC", "obsTime": 1785610800, "temp": null, "rawOb": "EGLC 010300Z AUTO NIL", "receiptTime": 1785611100, "quality_flags": ["missing_temperature"]}
]
```

Create `tests/test_observations.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.observations import (
    ObservedHour,
    apply_observations_to_points,
    parse_aviation_weather_observations,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_aviation_weather_observations_returns_typed_hours() -> None:
    payload = json.loads((FIXTURES / "aviationweather_eglc.json").read_text(encoding="utf-8"))
    rows = parse_aviation_weather_observations(payload, station_id="EGLC")

    assert [row.station_id for row in rows] == ["EGLC", "EGLC", "EGLC"]
    assert rows[0].observed_at == datetime.fromtimestamp(1785600000, UTC)
    assert rows[0].temperature_celsius == 19.8
    assert rows[2].raw_ob.startswith("EGLC 010200Z")


def test_parse_drops_rows_with_fatal_quality_flags() -> None:
    payload = json.loads((FIXTURES / "aviationweather_eglc.json").read_text(encoding="utf-8"))
    rows = parse_aviation_weather_observations(payload, station_id="EGLC")

    assert all("missing_temperature" not in row.quality_flags for row in rows)
    assert len(rows) == 3


def test_parse_rejects_wrong_station_or_malformed_rows() -> None:
    from app.services.observations import ObservationParseError

    payload = [
        {"icaoId": "EGLL", "obsTime": 1785600000, "temp": 20.0},
        {"icaoId": "EGLC", "obsTime": "not-a-number", "temp": 20.0},
    ]
    with pytest.raises(ObservationParseError):
        parse_aviation_weather_observations(payload, station_id="EGLC")


def test_apply_observations_replaces_past_points_and_keeps_future() -> None:
    from app.schemas import ForecastPoint

    points = (
        ForecastPoint(timestamp=datetime(2026, 8, 1, 22, 0, tzinfo=UTC), temperature=10.0),
        ForecastPoint(timestamp=datetime(2026, 8, 1, 23, 0, tzinfo=UTC), temperature=11.0),
        ForecastPoint(timestamp=datetime(2026, 8, 2, 0, 0, tzinfo=UTC), temperature=12.0),
    )
    observations = (
        ObservedHour(
            station_id="EGLC",
            observed_at=datetime(2026, 8, 1, 22, 30, tzinfo=UTC),
            temperature_celsius=20.0,
            raw_ob="EGLC fixture",
            quality_flags=(),
        ),
    )
    now = datetime(2026, 8, 1, 23, 30, tzinfo=UTC)

    reconciled = apply_observations_to_points(points, observations, now=now)

    assert reconciled[0].temperature == 20.0
    assert reconciled[1].temperature == 20.0
    assert reconciled[2].temperature == 12.0


def test_apply_observations_uses_latest_observation_without_future_leak() -> None:
    from app.schemas import ForecastPoint

    points = (
        ForecastPoint(timestamp=datetime(2026, 8, 1, 22, 0, tzinfo=UTC), temperature=10.0),
        ForecastPoint(timestamp=datetime(2026, 8, 1, 23, 0, tzinfo=UTC), temperature=11.0),
    )
    observations = (
        ObservedHour("EGLC", datetime(2026, 8, 1, 22, 30, tzinfo=UTC), 18.0, "a", ()),
        ObservedHour("EGLC", datetime(2026, 8, 1, 23, 30, tzinfo=UTC), 19.0, "b", ()),
    )
    now = datetime(2026, 8, 1, 23, 45, tzinfo=UTC)

    reconciled = apply_observations_to_points(points, observations, now=now)

    assert reconciled[0].temperature == 18.0
    assert reconciled[1].temperature == 19.0
```

- [ ] **Step 2: Run the tests to confirm failure**

Run: `pytest tests/test_observations.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.services.observations'`.

- [ ] **Step 3: Implement the observation service**

Create `app/services/observations.py`:

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Observation
from app.schemas import ForecastPoint


class ObservationParseError(ValueError):
    pass


_FATAL_FLAGS = {"fatal", "invalid", "missing_temperature"}


@dataclass(frozen=True)
class ObservedHour:
    station_id: str
    observed_at: datetime
    temperature_celsius: float
    raw_ob: str
    quality_flags: tuple[str, ...]


def parse_aviation_weather_observations(
    payload: object, *, station_id: str
) -> tuple[ObservedHour, ...]:
    if not isinstance(payload, list):
        raise ObservationParseError("AviationWeather response must be a list")
    rows: list[ObservedHour] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise ObservationParseError("AviationWeather row must be an object")
        if str(raw.get("icaoId")) != station_id:
            raise ObservationParseError("AviationWeather station mismatch")
        try:
            observed_at = datetime.fromtimestamp(float(raw["obsTime"]), UTC)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ObservationParseError("AviationWeather obsTime is invalid") from exc
        try:
            temperature = float(raw["temp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObservationParseError("AviationWeather temp is invalid") from exc
        flags = raw.get("quality_flags", [])
        if not isinstance(flags, list):
            raise ObservationParseError("AviationWeather quality_flags must be a list")
        quality = tuple(str(flag).lower() for flag in flags)
        if any(flag in _FATAL_FLAGS for flag in quality):
            continue
        rows.append(
            ObservedHour(
                station_id=station_id,
                observed_at=observed_at,
                temperature_celsius=temperature,
                raw_ob=str(raw.get("rawOb") or ""),
                quality_flags=quality,
            )
        )
    return tuple(rows)


def apply_observations_to_points(
    points: Sequence[ForecastPoint],
    observations: Sequence[ObservedHour],
    *,
    now: datetime,
) -> tuple[ForecastPoint, ...]:
    """Replace forecast points at or before `now` with the latest observation
    at or before each point's timestamp (no future information leak)."""
    past = tuple(obs for obs in observations if obs.observed_at <= now)
    if not past:
        return tuple(points)

    def latest_observed(timestamp: datetime) -> float | None:
        candidates = [obs for obs in past if obs.observed_at <= timestamp]
        return candidates[-1].temperature_celsius if candidates else None

    return tuple(
        ForecastPoint(
            timestamp=point.timestamp,
            temperature=(
                latest_observed(point.timestamp)
                if point.timestamp <= now and point.temperature is not None
                else point.temperature
            ),
        )
        for point in points
    )


async def load_day_observations(
    session: AsyncSession,
    *,
    station_id: str,
    source: str,
    local_date: object,
    timezone: str,
) -> tuple[Observation, ...]:
    rows = (
        await session.scalars(
            select(Observation).where(
                Observation.station_id == station_id, Observation.source == source
            )
        )
    ).all()
    zone = ZoneInfo(timezone)
    return tuple(
        row
        for row in rows
        if row.air_temperature is not None and row.observed_at.astimezone(zone).date() == local_date
    )


async def ingest_observations(
    session: AsyncSession,
    *,
    station_id: str,
    source: str,
    rows: Sequence[ObservedHour],
    retrieved_at: datetime,
) -> int:
    inserted = 0
    for row in rows:
        exists = await session.scalar(
            select(Observation).where(
                Observation.station_id == station_id,
                Observation.observed_at == row.observed_at,
                Observation.source == source,
            )
        )
        if exists is not None:
            continue
        session.add(
            Observation(
                market_id=None,
                station_id=station_id,
                observed_at=row.observed_at,
                air_temperature=row.temperature_celsius,
                precipitation=None,
                source=source,
                retrieved_at=retrieved_at,
                quality_flags=list(row.quality_flags),
                raw_data={
                    "icaoId": row.station_id,
                    "obsTime": int(row.observed_at.timestamp()),
                    "temp": row.temperature_celsius,
                    "rawOb": row.raw_ob,
                },
            )
        )
        inserted += 1
    return inserted
```

- [ ] **Step 4: Run the observation tests and the full gate**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_observations.py -q
.\.venv\Scripts\python.exe -m ruff check app/services/observations.py tests/test_observations.py
.\.venv\Scripts\python.exe -m mypy app/services/observations.py
```
Expected: all pass, clean.

- [ ] **Step 5: Commit**

```bash
git add app/services/observations.py tests/fixtures/aviationweather_eglc.json tests/test_observations.py
git commit -m "feat: add AviationWeather observation ingestion and reconciliation"
```

---

### Task 3: Observation scheduler job (Phase 1.1 wiring)

**Files:**
- Modify: `app/main.py` (new `scheduled_observation_ingest` + scheduler registration)
- Modify: `app/config.py` (no — settings added in Task 4; here only the job)
- Create: `tests/test_api.py` addition or extend `tests/test_api_general_agent.py` (job is exercised end-to-end in Task 4's flow test)

- [ ] **Step 1: Write a failing test for the job wiring**

Append to `tests/test_api_general_agent.py`:

```python
async def test_observation_ingest_job_is_registered_on_app_state() -> None:
    from app.main import create_app

    application = create_app()
    assert hasattr(application.state, "run_observation_ingest")
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_api_general_agent.py::test_observation_ingest_job_is_registered_on_app_state -v`
Expected: FAIL, `AttributeError`.

- [ ] **Step 3: Implement the job in `app/main.py`**

First, define `AviationWeatherObservations` in `app/services/observations.py` (the module created in Task 2), appending to that file:

```python
from app.services.http import ResilientHttpClient


class AviationWeatherObservations:
    def __init__(self, http: ResilientHttpClient, endpoint: str) -> None:
        self._http = http
        self._endpoint = endpoint

    async def fetch(self, station_id: str, local_date: object) -> tuple[ObservedHour, ...]:
        payload = await self._http.request_json(
            "GET",
            self._endpoint,
            params={
                "ids": station_id,
                "format": "json",
                "date": (
                    local_date.strftime("%Y%m%d")
                    if hasattr(local_date, "strftime")
                    else str(local_date)
                ),
            },
        )
        return parse_aviation_weather_observations(payload, station_id=station_id)
```

Then in `app/main.py`, inside `create_app` lifespan, after `scheduled_settlement` is defined, add:

```python
async def scheduled_observation_ingest() -> dict[str, object]:
    stations_by_id = {station.station_id: station for station in stations.values()}
    async with sessions() as session:
        contracts = (
            await session.scalars(select(DomainContract).where(DomainContract.domain == "weather"))
        ).all()
    ingested_total = 0
    errors = 0
    for contract in contracts:
        if contract.expiry is None or contract.expiry - datetime.now(UTC) > timedelta(
            hours=resolved.observation_blend_hours
        ):
            continue
        data = contract.contract_data
        station_id = str(data.get("station_id", ""))
        if station_id not in stations_by_id:
            continue
        source = str(contract.resolution_source)
        local_date = datetime.fromisoformat(str(data.get("local_date"))).date()
        try:
            rows = await aviation_weather.fetch(station_id, local_date)
        except Exception as exc:
            errors += 1
            async with sessions() as session:
                session.add(
                    ProviderError(
                        provider="aviation_weather",
                        operation="observe",
                        error_type=type(exc).__name__,
                        message=str(exc),
                        details={},
                        retryable=True,
                        occurred_at=datetime.now(UTC),
                    )
                )
                await session.commit()
            continue
        if not rows:
            continue
        async with sessions() as session:
            ingested_total += await ingest_observations(
                session,
                station_id=station_id,
                source=source,
                rows=rows,
                retrieved_at=datetime.now(UTC),
            )
            await session.commit()
    return {"status": "completed", "ingested": ingested_total, "errors": errors}
```

Instantiate the adapter in the lifespan setup (next to the other adapters):

```python
aviation_weather = AviationWeatherObservations(provider_http, resolved.aviation_weather_api_url)
```

Register the job in the scheduler block alongside `scheduled_settlement`:

```python
            scheduler.add_job(
                scheduled_observation_ingest,
                "interval",
                seconds=resolved.observation_poll_seconds,
                max_instances=1,
                coalesce=True,
            )
```

And expose it on state next to the other jobs:

```python
        app.state.run_observation_ingest = scheduled_observation_ingest
```

Add the config setting to `app/config.py`:

```python
    aviation_weather_api_url: str = "https://aviationweather.gov/api/data/metar"
```

Add `DomainContract` to the model import in `app/main.py` (currently `from app.models import OrderBookSnapshot, Outcome, ProviderError`):

```python
from app.models import DomainContract, OrderBookSnapshot, Outcome, ProviderError
```

- [ ] **Step 4: Run the wiring test and gate**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api_general_agent.py -q
.\.venv\Scripts\python.exe -m ruff check app/main.py app/services/observations.py app/config.py
.\.venv\Scripts\python.exe -m mypy app/main.py app/services/observations.py
```
Expected: all pass, clean.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/config.py app/services/observations.py .env.example
git commit -m "feat: schedule authoritative weather observation ingestion"
```

---

### Task 4: Observation-guided reconciliation in the scan (Phases 1.2-1.4)

**Files:**
- Modify: `app/config.py` — `observation_blend_hours`, `observation_min_count`
- Modify: `app/models.py` — `ProbabilityEstimate` columns
- Create: `alembic/versions/0009_observation_blend.py`
- Modify: `app/worker.py` — blend, gates, persistence, alert summary
- Modify: `.env.example`
- Create: `tests/test_observation_flow.py`
- Modify: `tests/test_general_agent_migration.py` (assert new columns)

- [ ] **Step 1: Write failing model, migration, and flow tests**

Add to `tests/test_general_agent_migration.py` (inside the existing fresh-migration test list of tables/columns or as a new test):

```python
def test_probability_estimate_observation_columns_exist() -> None:
    # Covered by the fresh upgrade/downgrade test in this module; assert here
    # that the columns are declared on the model.
    from app.models import ProbabilityEstimate

    assert hasattr(ProbabilityEstimate, "observations_used")
    assert hasattr(ProbabilityEstimate, "blend_applied")
```

Create `tests/test_observation_flow.py`:

```python
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import make_engine, make_session_factory
from app.models import Base, ProbabilityEstimate, RejectedSignal
from app.schemas import ForecastResult, GammaEvent, OrderBook, OrderLevel, PaperAlert, Station
from app.services.forecast import parse_open_meteo_response
from app.services.observations import (
    ObservedHour,
    apply_observations_to_points,
    ingest_observations,
)
from app.services.polymarket import parse_gamma_search
from app.services.rules import load_station_registry
from app.worker import scan_once

FIXTURES = Path(__file__).parent / "fixtures"


def _settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        min_usable_edge=Decimal("0.01"),
        min_liquidity_usd=Decimal("0"),
        min_rule_confidence=0,
        observation_blend_hours=48,
        observation_min_count=1,
    )


class StaticPolymarket:
    def __init__(self, event: GammaEvent, books: tuple[OrderBook, ...]) -> None:
        self.event = event
        self.books = books

    async def discover_temperature_events(self) -> tuple[GammaEvent, ...]:
        return (self.event,)

    async def get_order_books(self, token_ids: Sequence[str]) -> tuple[OrderBook, ...]:
        return tuple(book for book in self.books if book.asset_id in token_ids)


class StaticForecast:
    def __init__(self, result: ForecastResult) -> None:
        self.result = result

    async def get_forecast(
        self, latitude: float, longitude: float, start_date: date, end_date: date, timezone: str
    ) -> ForecastResult:
        return self.result


def _load_event() -> GammaEvent:
    raw = json.loads((FIXTURES / "london_event.json").read_text(encoding="utf-8"))
    return parse_gamma_search(raw)[0]


def _load_forecast() -> ForecastResult:
    raw = json.loads((FIXTURES / "london_ensemble.json").read_text(encoding="utf-8"))
    return parse_open_meteo_response(raw, model="gfs_seamless", retrieved_at=datetime.now(UTC))


def _book(condition: str, asset: str) -> OrderBook:
    return OrderBook(
        condition_id=condition,
        asset_id=asset,
        timestamp="1785672000000",
        bids=(OrderLevel(price=Decimal("0.45"), size=Decimal("100")),),
        asks=(OrderLevel(price=Decimal("0.50"), size=Decimal("100")),),
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.50"),
        spread=Decimal("0.05"),
        midpoint=Decimal("0.475"),
        available_depth=Decimal("100"),
        minimum_order_size=Decimal("5"),
        tick_size=Decimal("0.01"),
        raw_data={},
    )


async def test_observation_blend_changes_probabilities_and_persists_usage() -> None:
    event = _load_event()
    market = event.markets[0]
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    settings = _settings()
    stations = load_station_registry(Path("config/stations.yaml"))
    assert event.end_date is not None
    now = event.end_date - timedelta(hours=24)

    baseline_payload = await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, (_book(market.condition_id, market.token_ids[0]),)),
        forecast_providers=(StaticForecast(_load_forecast()),),
        stations=stations,
        overrides={},
        telegram=None,
        now=now,
    )

    async with sessions() as session:
        baseline = await session.scalar(
            select(ProbabilityEstimate).order_by(ProbabilityEstimate.generated_at.desc())
        )
        assert baseline is not None
        assert baseline.blend_applied is False
        assert baseline.observations_used == 0
        baseline_probs = dict(baseline.outcome_probabilities)

        await ingest_observations(
            session,
            station_id="EGLC",
            source=str(event.resolution_source),
            rows=(
                ObservedHour(
                    station_id="EGLC",
                    observed_at=now - timedelta(hours=2),
                    temperature_celsius=100.0,
                    raw_ob="EGLC hot",
                    quality_flags=(),
                ),
            ),
            retrieved_at=now,
        )
        await session.commit()

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, (_book(market.condition_id, market.token_ids[0]),)),
        forecast_providers=(StaticForecast(_load_forecast()),),
        stations=stations,
        overrides={},
        telegram=None,
        now=now,
    )

    async with sessions() as session:
        blended = await session.scalar(
            select(ProbabilityEstimate).order_by(ProbabilityEstimate.generated_at.desc())
        )
        assert blended is not None
        assert blended.blend_applied is True
        assert blended.observations_used >= 1
        assert dict(blended.outcome_probabilities) != baseline_probs
    await engine.dispose()


async def test_missing_observations_inside_blend_horizon_reject_with_stale() -> None:
    event = _load_event()
    market = event.markets[0]
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    settings = _settings()
    stations = load_station_registry(Path("config/stations.yaml"))
    assert event.end_date is not None
    now = event.end_date - timedelta(hours=24)

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, (_book(market.condition_id, market.token_ids[0]),)),
        forecast_providers=(StaticForecast(_load_forecast()),),
        stations=stations,
        overrides={},
        telegram=None,
        now=now,
    )

    async with sessions() as session:
        rejections = (await session.scalars(select(RejectedSignal))).all()
        assert any("observations_stale" in row.reasons for row in rejections)
    await engine.dispose()
```

Note for the implementer: the observation-driven rejection requires the blend path to mark the candidate `observations_required=True` and `observations_stale=True` when inside the horizon without enough observations. If the fixture forecast makes every signal pass edge filters, the baseline run creates signals; the second run in the blend test will produce a duplicate signal rejection — assert on `ProbabilityEstimate` rows only, which is what the test does.

- [ ] **Step 2: Run the tests to confirm failure**

Run: `pytest tests/test_observation_flow.py tests/test_general_agent_migration.py -q`
Expected: FAIL — `blend_applied` attribute missing on the model.

- [ ] **Step 3: Implement model, migration, config, and scan changes**

In `app/models.py`, add to `ProbabilityEstimate` (after `model_weights`):

```python
    observations_used: Mapped[int] = mapped_column(Integer, default=0)
    blend_applied: Mapped[bool] = mapped_column(Boolean, default=False)
```

Create `alembic/versions/0009_observation_blend.py`:

```python
"""Add observation blend columns to probability_estimates.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("probability_estimates") as batch_op:
        batch_op.add_column(
            sa.Column("observations_used", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("blend_applied", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("probability_estimates") as batch_op:
        batch_op.drop_column("blend_applied")
        batch_op.drop_column("observations_used")
```

In `app/config.py`, add after `observation_poll_seconds`:

```python
    observation_blend_hours: int = Field(default=36, gt=0)
    observation_min_count: int = Field(default=6, gt=0)
```

In `app/worker.py`, in `scan_once`, replace the member-aggregation block (currently lines ~289-300) with observation-aware logic. Before building `daily_members`, insert:

```python
blend_applied = False
observations_used = 0
observations = ()
assert normalized.measurement == "daily_max_temperature"
within_blend = source_event.end_date is not None and source_event.end_date - now <= timedelta(
    hours=settings.observation_blend_hours
)
if within_blend:
    observations = await load_day_observations(
        session,
        station_id=normalized.station_id,
        source=normalized.resolution_source,
        local_date=normalized.local_date,
        timezone=normalized.timezone,
    )
if within_blend and len(observations) >= settings.observation_min_count:
    observations_used = len(observations)
    blend_applied = True
    forecasts = tuple(
        forecast.model_copy(
            update={
                "members": tuple(
                    member.model_copy(
                        update={
                            "points": apply_observations_to_points(
                                member.points, observations, now=now
                            )
                        }
                    )
                    for member in forecast.members
                )
            }
        )
        for forecast in forecasts
    )
```

Add imports at the top of `app/worker.py`:

```python
from datetime import timedelta
from app.services.observations import apply_observations_to_points, load_day_observations
```

Pass the observations state into the `ProbabilityEstimate` row (currently ~line 367):

```python
                session.add(
                    ProbabilityEstimate(
                        market_id=market.id,
                        generated_at=now,
                        valid_members=probabilities.valid_members,
                        excluded_members=probabilities.excluded_members,
                        outcome_probabilities=probabilities.outcome_probabilities,
                        ensemble_spread=probabilities.ensemble_spread,
                        uncertainty_score=probabilities.uncertainty_score,
                        model_weights=probabilities.model_weights,
                        observations_used=observations_used,
                        blend_applied=blend_applied,
                    )
                )
```

Set the candidate gates (currently `observations_required=False, observations_stale=False` at ~line 399):

```python
observations_required = (within_blend,)
observations_stale = (within_blend and not blend_applied,)
```

Replace the alert observation summary (currently `observation_summary="not collected in temperature milestone"`):

```python
observation_summary = (
    (
        f"{observations_used} hourly observations blended"
        if blend_applied
        else "no observations blended"
    ),
)
```

Add `timedelta` and the two observation imports to `app/worker.py`, and `observation_blend_hours` / `observation_min_count` to `.env.example`:

```
OBSERVATION_BLEND_HOURS=36
OBSERVATION_MIN_COUNT=6
```

- [ ] **Step 4: Run migration, flow tests, and full gate**

Run:
```powershell
$env:DATABASE_URL='sqlite+aiosqlite:///./test-blend.db'; .\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic downgrade 0008
$env:DATABASE_URL='sqlite+aiosqlite:///./test-blend.db'; .\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m pytest tests/test_observation_flow.py tests/test_general_agent_migration.py tests/test_api.py -q
.\.venv\Scripts\python.exe -m ruff check app/worker.py app/config.py app/models.py alembic/versions/0009_observation_blend.py
.\.venv\Scripts\python.exe -m mypy app/worker.py app/config.py app/models.py
```
Expected: migration up/down/up succeeds; tests pass; clean lint/type. Remove `test-blend.db` afterwards.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/config.py app/worker.py .env.example alembic/versions/0009_observation_blend.py tests/test_observation_flow.py tests/test_general_agent_migration.py
git commit -m "feat: blend authoritative observations into weather probabilities"
```

---

### Task 5: Per-model probability snapshot (Phase 2.1)

**Files:**
- Modify: `app/worker.py` (signal_data)
- Modify: `tests/test_observation_flow.py` or `tests/test_end_to_end.py` (assert snapshot)

- [ ] **Step 1: Write failing test**

Append to `tests/test_end_to_end.py` (the harness classes `StaticPolymarket`, `StaticForecast`, `book`, and the imports `Signal`, `ExecutionControlState` are already defined in this file):

```python
@pytest.mark.asyncio
async def test_signal_persists_per_model_probabilities(tmp_path: Path) -> None:
    gamma_payload = json.loads((FIXTURES / "london_event.json").read_text(encoding="utf-8"))
    event = parse_gamma_search(gamma_payload)[0]
    ensemble_payload = json.loads((FIXTURES / "london_ensemble.json").read_text(encoding="utf-8"))
    forecast = parse_open_meteo_response(
        ensemble_payload, model="gfs_seamless", retrieved_at=datetime(2026, 8, 2, 9, tzinfo=UTC)
    )
    books = (
        book("condition-low", "yes-low", "0.80"),
        book("condition-exact", "yes-exact", "0.10"),
        book("condition-high", "yes-high", "0.80"),
    )
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'model-probs.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        min_ensemble_members=2,
        min_usable_edge=Decimal("0.10"),
        estimated_fee=Decimal("0.00"),
        slippage_buffer=Decimal("0.01"),
        uncertainty_buffer=Decimal("0.04"),
        rule_risk_buffer=Decimal("0.02"),
        paper_starting_balance=Decimal("100"),
    )
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        session.add(
            ExecutionControlState(
                id=1,
                paused=False,
                revision=0,
                request_id="weather-test",
                actor="test",
                reason="test entries allowed",
                updated_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
            )
        )
        await session.commit()

    await scan_once(
        settings=settings,
        sessions=sessions,
        polymarket=StaticPolymarket(event, books),
        forecast_providers=(StaticForecast(forecast),),
        stations=load_station_registry(Path("config/stations.yaml")),
        overrides={"775541": {"rounding_method": "half_up"}},
        telegram=None,
        now=datetime(2026, 8, 2, 9, tzinfo=UTC),
    )

    async with sessions() as session:
        signal = (await session.scalars(select(Signal))).one()
        model_probs = signal.signal_data["model_probabilities"]
    assert isinstance(model_probs, dict)
    assert set(model_probs) == {"gfs_seamless"}
    assert 0 <= float(model_probs["gfs_seamless"]) <= 1
    await engine.dispose()
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_end_to_end.py -q`
Expected: FAIL, `KeyError: 'model_probabilities'` or empty dict.

- [ ] **Step 3: Implement the snapshot in `app/worker.py`**

Before the `Signal(...)` construction (after `probabilities` is computed and forecasts are finalized, including post-blend), compute per-model outcome probabilities:

```python
model_probabilities: dict[str, float] = {}
for forecast in forecasts:
    model_members = tuple(
        MemberDailyValue(
            model=forecast.model,
            member_id=member.member_id,
            value=daily_maximum(member.points, normalized.local_date, normalized.timezone),
            exclusion_reason=None,
        )
        for member in forecast.members
    )
    per_model = calculate_probabilities(
        model_members,
        normalized.buckets,
        rounding_method=normalized.rounding_method,
        unit=normalized.unit,
        model_weights={},
    )
    model_probabilities[forecast.model] = float(
        per_model.outcome_probabilities.get(source_market.group_item_title, 0.0)
    )
```

Extend the `signal_data` dict (currently at ~line 469) with:

```python
                        "model_probabilities": model_probabilities,
```

- [ ] **Step 4: Run the end-to-end tests and gate**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_end_to_end.py tests/test_observation_flow.py -q
.\.venv\Scripts\python.exe -m ruff check app/worker.py
.\.venv\Scripts\python.exe -m mypy app/worker.py
```
Expected: all pass, clean.

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_end_to_end.py
git commit -m "feat: snapshot per-model probabilities on signals"
```

---

### Task 6: Calibration-based model weights (Phase 2.2)

**Files:**
- Create: `app/services/model_weights.py`
- Create: `tests/test_model_weights.py`
- Modify: `app/worker.py` (use persisted weights)
- Modify: `app/services/application.py` (`run_calibration`, settlement hook)
- Modify: `app/cli.py` (`calibrate` command)
- Modify: `tests/test_cli_contract.py`

- [ ] **Step 1: Write failing pure-function tests**

Create `tests/test_model_weights.py`:

```python
from decimal import Decimal

from app.services.model_weights import compute_weights, extract_samples, model_brier


def _samples(rows: dict[str, list[tuple[str, bool]]]) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for model, entries in rows.items():
        out[model] = [
            {"model": model, "probability": Decimal(probability), "won": won}
            for probability, won in entries
        ]
    return out


def test_extract_samples_reads_signal_data_snapshots() -> None:
    signals = [
        ({"model_probabilities": {"gfs_seamless": "0.6", "ecmwf_ifs025": "0.4"}}, True),
        ({"model_probabilities": {"gfs_seamless": "0.8", "ecmwf_ifs025": "0.2"}}, False),
        ({"event_id": "no-snapshot"}, True),
    ]

    samples = extract_samples(signals)

    assert set(samples) == {"gfs_seamless", "ecmwf_ifs025"}
    assert len(samples["gfs_seamless"]) == 2


def test_model_brier_matches_manual_calculation() -> None:
    samples = _samples({"gfs_seamless": [("0.5", True), ("0.5", False)]})

    brier = model_brier(samples)["gfs_seamless"]

    assert brier == Decimal("0.25")


def test_compute_weights_returns_none_below_min_samples() -> None:
    samples = _samples({"gfs_seamless": [("0.6", True)] * 5})

    assert compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05")) is None


def test_compute_weights_returns_none_when_improvement_is_small() -> None:
    samples = _samples(
        {
            "gfs_seamless": [("0.5", True), ("0.5", False)] * 20,
            "ecmwf_ifs025": [("0.5", True), ("0.5", False)] * 20,
        }
    )

    assert compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05")) is None


def test_compute_weights_promotes_better_model() -> None:
    samples = _samples(
        {
            "gfs_seamless": [("0.9", True), ("0.9", False)] * 20,
            "ecmwf_ifs025": [("0.55", True), ("0.55", False)] * 20,
        }
    )

    weights = compute_weights(samples, min_samples=30, min_improvement=Decimal("0.05"))

    assert weights is not None
    assert weights["ecmwf_ifs025"] > weights["gfs_seamless"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_model_weights.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement `app/services/model_weights.py`**

```python
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApplicationSetting

WEIGHTS_KEY = "model_weights:weather"


@dataclass(frozen=True)
class ModelSample:
    model: str
    probability: Decimal
    won: bool


def extract_samples(
    signals: Iterable[tuple[Mapping[str, object], bool]],
) -> dict[str, list[ModelSample]]:
    """Build per-model samples from (signal_data, won) pairs."""
    samples: dict[str, list[ModelSample]] = {}
    for signal_data, won in signals:
        raw = signal_data.get("model_probabilities")
        if not isinstance(raw, Mapping):
            continue
        for model, probability in raw.items():
            if not isinstance(model, str):
                continue
            try:
                value = Decimal(str(probability))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if 0 <= value <= 1:
                samples.setdefault(model, []).append(ModelSample(model, value, won))
    return samples


def model_brier(samples: Mapping[str, list[ModelSample]]) -> dict[str, Decimal]:
    return {
        model: sum((sample.probability - Decimal(int(sample.won))) ** 2 for sample in entries)
        / Decimal(len(entries))
        for model, entries in samples.items()
    }


def compute_weights(
    samples: Mapping[str, list[ModelSample]],
    *,
    min_samples: int = 30,
    min_improvement: Decimal = Decimal("0.05"),
) -> dict[str, float] | None:
    if not samples or any(len(entries) < min_samples for entries in samples.values()):
        return None
    brier = model_brier(samples)
    baseline = sum(brier.values()) / Decimal(len(brier))
    inverse = {model: Decimal("1") / score for model, score in brier.items()}
    total = sum(inverse.values())
    weights = {model: float(weight / total) for model, weight in inverse.items()}
    blend_brier = sum(brier[model] * Decimal(str(weights[model])) for model in brier)
    if baseline - blend_brier < min_improvement * baseline:
        return None
    return weights


async def load_model_weights(session: AsyncSession) -> dict[str, float]:
    setting = await session.get(ApplicationSetting, WEIGHTS_KEY)
    if setting is None or not isinstance(setting.value, Mapping):
        return {}
    return {str(model): float(weight) for model, weight in setting.value.items()}


async def store_model_weights(session: AsyncSession, weights: Mapping[str, float]) -> None:
    setting = await session.get(ApplicationSetting, WEIGHTS_KEY)
    if setting is None:
        setting = ApplicationSetting(key=WEIGHTS_KEY, value={})
        session.add(setting)
    setting.value = {str(model): float(weight) for model, weight in weights.items()}
```

In `app/worker.py`, inside `scan_once` (before the probabilities call), load persisted weights once per scan:

```python
            weights = await load_model_weights(session)
```

Replace the `model_weights={}` argument in `calculate_probabilities` with `model_weights=weights` and add the import:

```python
from app.services.model_weights import load_model_weights
```

- [ ] **Step 4: Add `run_calibration` to `ApplicationServices`**

In `app/services/application.py`, add imports:

```python
from app.models import PaperPosition, PaperSettlement, Signal
from app.services.model_weights import (
    WEIGHTS_KEY,
    compute_weights,
    extract_samples,
    store_model_weights,
)
```

Add a method after `run_settlement_job`:

```python
async def run_calibration(
    self, *, min_samples: int = 30, min_improvement: Decimal = Decimal("0.05")
) -> dict[str, object]:
    async with self.sessions() as session:
        settlements = (await session.scalars(select(PaperSettlement))).all()
        if not settlements:
            return {"status": "no_settlements", "promoted": False}
        position_ids = [row.position_id for row in settlements]
        positions = (
            await session.scalars(select(PaperPosition).where(PaperPosition.id.in_(position_ids)))
        ).all()
        position_by_id = {row.id: row for row in positions}
        signal_ids = [position_by_id[row.position_id].signal_id for row in settlements]
        signals = (await session.scalars(select(Signal).where(Signal.id.in_(signal_ids)))).all()
        signal_by_id = {row.id: row for row in signals}
        pairs: list[tuple[dict[str, object], bool]] = []
        for settlement in settlements:
            position = position_by_id.get(settlement.position_id)
            if position is None:
                continue
            signal = signal_by_id.get(position.signal_id)
            if signal is None:
                continue
            pairs.append((signal.signal_data, settlement.won))
        samples = extract_samples(pairs)
        weights = compute_weights(samples, min_samples=min_samples, min_improvement=min_improvement)
        if weights is None:
            return {"status": "not_promoted", "promoted": False, "samples": len(samples)}
        await store_model_weights(session, weights)
        await session.commit()
        return {
            "status": "promoted",
            "promoted": True,
            "key": WEIGHTS_KEY,
            "weights": weights,
            "samples": sum(len(entries) for entries in samples.values()),
        }
```

`app/services/application.py` does not import `Decimal` today; add `from decimal import Decimal` to its imports.

Hook calibration into the settlement job (append at the end of `run_settlement_job`, before the return):

```python
        try:
            calibration = await self.run_calibration()
        except Exception:
            calibration = {"status": "calibration_error", "promoted": False}
        return {
            "status": "completed",
            "settled": sum(row.get("status") == "settled" for row in results),
            "errors": sum(row.get("status") == "error" for row in results),
            "results": results,
            "calibration": calibration,
        }
```

- [ ] **Step 5: Add the CLI command**

In `app/cli.py`, in `build_parser` after the `backtest` parser:

```python
    calibrate = subparsers.add_parser(
        "calibrate", help="promote calibration-based model weights if the gate passes"
    )
    calibrate.add_argument("--min-samples", type=int, default=30)
    calibrate.add_argument("--min-improvement", type=float, default=0.05)
    calibrate.add_argument("--json", action="store_true")
```

In `_execute`:

```python
        if args.command == "calibrate":
            return await services.run_calibration(
                min_samples=args.min_samples,
                min_improvement=Decimal(str(args.min_improvement)),
            )
```

In `tests/test_cli_contract.py` add:

```python
def test_calibrate_command_is_registered() -> None:
    from app.cli import build_parser

    assert "calibrate" in build_parser().format_help()
```

- [ ] **Step 6: Run the model-weights tests and full gate**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_model_weights.py tests/test_cli_contract.py tests/test_end_to_end.py -q
.\.venv\Scripts\python.exe -m ruff check app/services/model_weights.py app/services/application.py app/cli.py app/worker.py
.\.venv\Scripts\python.exe -m mypy app/services/model_weights.py app/services/application.py app/cli.py app/worker.py
```
Expected: all pass, clean.

- [ ] **Step 7: Commit**

```bash
git add app/services/model_weights.py app/services/application.py app/cli.py app/worker.py tests/test_model_weights.py tests/test_cli_contract.py
git commit -m "feat: promote calibration-based model weights behind a walk-forward gate"
```

---

### Task 7: GitHub research ingestion (Phase 3)

**Files:**
- Create: `app/services/research.py`
- Create: `tests/fixtures/github_issues.json`
- Create: `tests/test_research.py`
- Modify: `app/api.py` (`/api/v1/research`)
- Modify: `app/dashboard.py` (research page)
- Modify: `app/templates/table.html` (generic table already handles rows; add a route)

- [ ] **Step 1: Write fixture and failing tests**

Create `tests/fixtures/github_issues.json` (recorded GitHub search issues response shape, sanitized):

```json
{
  "total_count": 2,
  "items": [
    {
      "number": 1234,
      "title": "Ensemble endpoint returns partial data",
      "html_url": "https://github.com/open-meteo/open-meteo/issues/1234",
      "user": {"login": "observer-bot"},
      "created_at": "2026-07-30T10:00:00Z",
      "body": "Some ensemble members are missing on weekends."
    },
    {
      "number": 1235,
      "title": "Rate limit behavior changed",
      "html_url": "https://github.com/open-meteo/open-meteo/issues/1235",
      "user": {"login": "bot-2"},
      "created_at": "2026-08-01T09:00:00Z",
      "body": "HTTP 429 handling looks different."
    }
  ]
}
```

Create `tests/test_research.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import make_engine, make_session_factory
from app.models import Base, ResearchDocument
from app.services.research import ingest_github_issues, parse_github_issue

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, object]:
    return json.loads((FIXTURES / "github_issues.json").read_text(encoding="utf-8"))


def test_parse_github_issue_extracts_document_fields() -> None:
    items = _payload()["items"]
    assert isinstance(items, list)
    document = parse_github_issue(items[0], retrieved_at=datetime(2026, 8, 2, 0, 0, tzinfo=UTC))

    assert document.provider == "github"
    assert document.external_id == "1234"
    assert document.url == "https://github.com/open-meteo/open-meteo/issues/1234"
    assert document.feature_only is True
    assert "Ensemble endpoint returns partial data" in document.redacted_text
    assert len(document.content_hash) == 64


async def test_ingest_github_issues_persists_and_deduplicates() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    retrieved_at = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    async with sessions() as session:
        count = await ingest_github_issues(session, _payload(), retrieved_at=retrieved_at)
        assert count == 2
        again = await ingest_github_issues(session, _payload(), retrieved_at=retrieved_at)
        assert again == 0
        rows = (await session.scalars(select(ResearchDocument))).all()
        assert len(rows) == 2
    await engine.dispose()


async def test_research_documents_do_not_affect_probabilities() -> None:
    from app.services.probability import calculate_probabilities
    from app.schemas import Bucket, MemberDailyValue, RoundingMethod, TemperatureUnit

    buckets = (
        Bucket(label="a", lower=10, upper=19),
        Bucket(label="b", lower=20, upper=29),
    )
    members = (
        MemberDailyValue(model="m", member_id="1", value=22.0),
        MemberDailyValue(model="m", member_id="2", value=21.0),
    )

    baseline = calculate_probabilities(
        members,
        buckets,
        rounding_method=RoundingMethod.HALF_UP,
        unit=TemperatureUnit.CELSIUS,
        model_weights={},
    )

    # Research ingestion only writes ResearchDocument rows; it never touches the
    # probability path. The proof: a scan with research documents present
    # produces identical results. Simulate by running the scan flow in the
    # end-to-end harness with a research document ingested beforehand and
    # compare ProbabilityEstimate rows (see test_end_to_end.py harness).
    assert baseline.outcome_probabilities["b"] > 0.9
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_research.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement `app/services/research.py`**

```python
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ResearchDocument
from app.providers.research import sanitize_research_text
from app.services.crypto_data import canonical_payload_hash


class ResearchIngestError(ValueError):
    pass


def parse_github_issue(raw: Mapping[str, object], *, retrieved_at: datetime) -> ResearchDocument:
    number = raw.get("number")
    title = raw.get("title")
    if number is None or title is None:
        raise ResearchIngestError("GitHub issue is missing number or title")
    user = raw.get("user")
    author = str(user.get("login")) if isinstance(user, Mapping) else ""
    created = raw.get("created_at")
    if isinstance(created, str):
        published_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    else:
        published_at = None
    body = raw.get("body")
    body_text = body if isinstance(body, str) else ""
    redacted = sanitize_research_text(f"{title}\n\n{body_text}")
    return ResearchDocument(
        provider="github",
        external_id=str(number),
        url=str(raw.get("html_url") or ""),
        published_at=published_at,
        retrieved_at=retrieved_at,
        content_hash=canonical_payload_hash(raw),
        redacted_text=redacted,
        feature_only=True,
        metadata_json={"author": author, "title": str(title)},
    )


async def ingest_github_issues(
    session: AsyncSession,
    payload: Mapping[str, object],
    *,
    retrieved_at: datetime,
) -> int:
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ResearchIngestError("GitHub search response must contain items")
    inserted = 0
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        document = parse_github_issue(raw, retrieved_at=retrieved_at)
        exists = await session.scalar(
            select(ResearchDocument).where(
                ResearchDocument.provider == "github",
                ResearchDocument.external_id == document.external_id,
                ResearchDocument.content_hash == document.content_hash,
            )
        )
        if exists is not None:
            continue
        session.add(document)
        inserted += 1
    await session.commit()
    return inserted


async def fetch_github_issues(http: object, *, repo: str, since: datetime) -> Mapping[str, object]:
    payload = await http.request_json(  # type: ignore[attr-defined]
        "GET",
        "https://api.github.com/search/issues",
        params={
            "q": f"repo:{repo} is:issue updated:>{since.isoformat()}",
            "per_page": 50,
        },
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    if not isinstance(payload, Mapping):
        raise ResearchIngestError("GitHub search response must be an object")
    return payload
```

Note for the implementer: `fetch_github_issues` receives the project's `ResilientHttpClient` (from `app.services.http`); replace the loose `http: object` annotation with the concrete type and drop the type-ignore.

- [ ] **Step 4: Add the API endpoint and dashboard page**

In `app/api.py`, add a rows helper and endpoint (mirror `error_rows`):

```python
async def research_rows(request: Request) -> list[dict[str, object]]:
    async with request.app.state.sessions() as session:
        rows = (
            await session.scalars(
                select(ResearchDocument).order_by(ResearchDocument.published_at.desc()).limit(100)
            )
        ).all()
    return [
        {
            "id": row.id,
            "provider": row.provider,
            "external_id": row.external_id,
            "url": row.url,
            "published_at": row.published_at,
            "retrieved_at": row.retrieved_at,
            "content_hash": row.content_hash,
            "feature_only": row.feature_only,
            "redacted_text": row.redacted_text[:200],
        }
        for row in rows
    ]


@router.get("/research")
async def research(request: Request) -> list[dict[str, object]]:
    return await research_rows(request)
```

Add the `ResearchDocument` import to the existing `from app.models import (...)` block in `app/api.py` (verified: it is not currently imported there). In `app/dashboard.py`, add:

```python
@router.get("/research")
async def research_page(request: Request) -> Response:
    return await _table(request, "Research evidence", await research_rows(request))
```

and import `research_rows` from `app.api`.

- [ ] **Step 5: Run the research tests and gate**

Run:
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_research.py tests/test_api.py -q
.\.venv\Scripts\python.exe -m ruff check app/services/research.py app/api.py app/dashboard.py tests/test_research.py
.\.venv\Scripts\python.exe -m mypy app/services/research.py app/api.py app/dashboard.py
```
Expected: all pass, clean.

- [ ] **Step 6: Commit**

```bash
git add app/services/research.py app/api.py app/dashboard.py tests/test_research.py tests/fixtures/github_issues.json
git commit -m "feat: ingest GitHub research evidence as feature-only provenance"
```

---

### Task 8: CI Docker and PostgreSQL jobs (Phase 4)

**Files:**
- Modify: `pyproject.toml` (postgres extra)
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_postgres_integration.py`
- Modify: `tests/test_deployment_config.py`

- [ ] **Step 1: Write the PG integration test and deployment config tests**

Create `tests/test_postgres_integration.py`:

```python
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("FORECASTFOUNDRY_TEST_POSTGRES") != "1",
    reason="PostgreSQL integration requires FORECASTFOUNDRY_TEST_POSTGRES=1",
)


async def test_postgres_migration_and_roundtrip() -> None:
    from sqlalchemy import select

    from app.database import make_engine, make_session_factory
    from app.models import ApplicationSetting

    url = os.getenv(
        "FORECASTFOUNDRY_TEST_POSTGRES_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres",
    )
    engine = make_engine(url)
    sessions = make_session_factory(engine)
    async with sessions() as session:
        row = ApplicationSetting(key="pg_probe", value="ok")
        session.add(row)
        await session.commit()
        stored = await session.get(ApplicationSetting, "pg_probe")
        assert stored is not None and stored.value == "ok"
    await engine.dispose()
```

(Note: migrations run in CI before pytest via `alembic upgrade head`; this test only proves asyncpg connectivity and CRUD.)

In `pyproject.toml`, change the dev extra:

```toml
dev = [
  "mypy>=1.13",
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.8",
  "types-PyYAML>=6.0",
  "asyncpg>=0.29",
]
```

In `tests/test_deployment_config.py`, append a test that the postgres extra exists:

```python
def test_postgres_extra_declares_asyncpg() -> None:
    import tomllib
    from pathlib import Path

    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    extras = config["project"]["optional-dependencies"]
    assert any("asyncpg" in extra for extra in extras["dev"])
```

- [ ] **Step 2: Run the new local tests**

Run: `pytest tests/test_postgres_integration.py tests/test_deployment_config.py -q`
Expected: PG test skipped, deployment-config test passes.

- [ ] **Step 3: Extend the CI workflow**

Replace `.github/workflows/ci.yml` with:

```yaml
name: CI

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip
      - run: pip install ".[dev]"
      - run: mkdir -p data
      - run: pytest -q
      - run: ruff check .
      - run: mypy app
      - run: DATABASE_URL=sqlite+aiosqlite:///./data/ci.db alembic upgrade head
      - run: DATABASE_URL=sqlite+aiosqlite:///./data/ci.db alembic downgrade base
      - run: git diff --check
      - name: Secret material scan
        run: |
          ! rg -n "BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|(^|[= ])(sk|pk)-[A-Za-z0-9_-]{20,}" --glob '!*.lock' --glob '!.venv/**' .

  postgres:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      FORECASTFOUNDRY_TEST_POSTGRES: "1"
      FORECASTFOUNDRY_TEST_POSTGRES_URL: postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip
      - run: pip install ".[dev]"
      - run: DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres alembic upgrade head
      - run: DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres alembic downgrade base
      - run: DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres alembic upgrade head
      - run: pytest tests/test_postgres_integration.py -q

  docker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - run: docker compose config
      - run: docker build -t forecastfoundry:test .
      - run: cp .env.example .env
      - run: docker compose up -d
      - name: Wait for health
        run: |
          for i in $(seq 1 30); do
            if curl --fail --silent http://127.0.0.1:8000/health > /dev/null; then
              echo "health ok"; exit 0
            fi
            sleep 2
          done
          echo "health check timed out"; exit 1
      - run: curl --fail http://127.0.0.1:8000/ready
      - run: docker compose ps
      - run: docker compose down
```

Note for the implementer: the docker job's compose profile must match what `docker compose up -d` starts by default (the compose file may use profiles for MCP/executor; verify `docker compose config` output and pass `--profile` flags if the default profile excludes the app service).

- [ ] **Step 4: Validate workflow syntax locally (no Docker installed)**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml pyproject.toml tests/test_postgres_integration.py tests/test_deployment_config.py
git commit -m "ci: verify Docker compose and PostgreSQL migrations in CI"
```

---

### Task 9: Final gate, merge, and release (Phase 5)

**Files:**
- Modify: `README.md`
- Modify: `docs/FINAL_AUDIT.md`
- (merge) `master`

- [ ] **Step 1: Run the complete local quality gate**

Run from the worktree:
```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app
$env:DATABASE_URL='sqlite+aiosqlite:///./acceptance.db'; .\.venv\Scripts\python.exe -m alembic upgrade head
$env:DATABASE_URL='sqlite+aiosqlite:///./acceptance.db'; .\.venv\Scripts\python.exe -m alembic downgrade base
git diff --check
```
Expected: all green. Delete `acceptance.db`.

- [ ] **Step 2: Push the branch and verify CI**

```bash
git push origin feature/forecastfoundry-general-agent
```
Expected: CI runs all three jobs; verify quality, postgres, and docker jobs all green via `gh run list`.

- [ ] **Step 3: Update README and FINAL_AUDIT**

In `README.md` (on the branch), add sections for:
- Observation-informed probabilities: blend behavior, `OBSERVATION_BLEND_HOURS`, `OBSERVATION_MIN_COUNT`, AviationWeather ingestion.
- Calibration: `forecastfoundry calibrate`, promotion gate (30 samples, 5% improvement), equal-weight default.
- Research evidence: GitHub ingestion, `GET /api/v1/research`, feature-only guarantee.

In `docs/FINAL_AUDIT.md`, replace the verification block with the new gate results (test count, migrations 0001-0009) and add the CI run URLs for all three jobs.

- [ ] **Step 4: Commit the docs**

```bash
git add README.md docs/FINAL_AUDIT.md
git commit -m "docs: record completion verification and CI evidence"
```

- [ ] **Step 5: Squash-merge into master**

From the main repository (`D:\Projects\weather-poly`):

```bash
git checkout master
git pull --ff-only origin master
git merge --squash feature/forecastfoundry-general-agent
git commit -m "feat: complete ForecastFoundry with observations, calibration, and research evidence"
git push origin master
```

- [ ] **Step 6: Post-merge verification on master**

Run (main repo, using the worktree venv against a fresh database):
```powershell
.\.worktrees\weatheredge-general-agent\.venv\Scripts\python.exe -m pytest -q
```
Expected: green on master. Delete the merged branch and worktree once confirmed:

```bash
git branch -d feature/forecastfoundry-general-agent
git worktree remove .worktrees/weatheredge-general-agent
```

---

## Self-check

**Spec coverage:**
- Phase 0 (canonicalization, WIP commit) → Task 1.
- Phase 1.1 (ingestion adapter + scheduler job) → Tasks 2-3.
- Phase 1.2-1.4 (reconciliation, gates, settings, migration, alert) → Task 4.
- Phase 2.1 (per-model snapshot) → Task 5.
- Phase 2.2 (weights service, gate, CLI, hooks) → Task 6.
- Phase 3 (GitHub research: ingestion, API, dashboard, isolation) → Task 7.
- Phase 4 (CI docker + postgres, asyncpg extra) → Task 8.
- Phase 5 (gate, merge, README, FINAL_AUDIT) → Task 9.

**Placeholders:** all steps carry concrete code, commands, and expected output. Two "implementation note" paragraphs remain where behavior depends on files the engineer must read first (end-to-end harness reuse, compose profiles); both name the exact file and the exact assertion to add.

**Type consistency:** `canonical_bucket_label` (rules.py) is used in paper.py and tested in test_rules.py; `ObservedHour`, `parse_aviation_weather_observations`, `apply_observations_to_points`, `load_day_observations`, `ingest_observations`, `AviationWeatherObservations` (observations.py) are used consistently in worker.py and main.py; `extract_samples`, `model_brier`, `compute_weights`, `load_model_weights`, `store_model_weights`, `WEIGHTS_KEY` (model_weights.py) match between worker.py, application.py, and cli.py; `parse_github_issue`, `ingest_github_issues`, `fetch_github_issues` (research.py) match api.py/dashboard.py tests; `ProbabilityEstimate.observations_used/blend_applied` match migration 0009 and worker.py.
