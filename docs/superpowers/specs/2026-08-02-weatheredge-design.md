# WeatherEdge Initial Milestone Design

## Scope

WeatherEdge version 1 is a read-only Polymarket weather-market intelligence service. The initial milestone supports active daily maximum-temperature bucket events, public market and order-book data, Open-Meteo ensemble forecasts, deterministic probabilities, edge filtering, a $5 paper account, outbound Telegram alerts, SQLite persistence, a small server-rendered dashboard, a JSON API, and Docker deployment.

The milestone does not implement real trading, wallet handling, authenticated Polymarket endpoints, rainfall, snow, hurricanes, official observation providers, Telegram command polling, or automatic settlement. Those features are later milestones and must not be represented by placeholder functions.

## Architecture

One FastAPI process owns the HTTP API, dashboard, async SQLAlchemy engine, and APScheduler jobs. Polling is authoritative. An optional public Polymarket market WebSocket may refresh order-book state sooner, but the periodic polling path remains sufficient on its own.

The processing flow is:

1. Discover active temperature events and their binary bucket markets through the public Gamma API.
2. Persist exact questions, descriptions, resolution sources, condition IDs, outcome labels, and CLOB token IDs.
3. Normalize resolution rules deterministically using the event description, individual bucket title, audited station registry, and optional explicit market override.
4. Reject any candidate whose station, coordinates, date, timezone, measurement, unit, precision, rounding, or complete sibling-bucket structure cannot be established confidently.
5. Fetch each YES token's public CLOB book and calculate the executable best ask, best bid, spread, midpoint, quoted depth, and minimum order size. The midpoint is never used as an executable price.
6. Fetch individual hourly Open-Meteo ensemble members for the exact local date and coordinates.
7. Calculate each member's local-calendar daily maximum, apply configured station/model bias, apply the market rounding rule, and map the result into exactly one bucket.
8. Calculate per-bucket probabilities without collapsing model members into a deterministic average.
9. Evaluate rule confidence, price, spread, liquidity, member count, data-quality flags, uncertainty, and configurable buffers.
10. Persist either an accepted signal or explicit rejection reasons in one database transaction.
11. Deduplicate accepted alerts, simulate an affordable paper entry when possible, and send an optional Telegram message.

Startup fails if `REAL_TRADING_ENABLED` is true. The source tree contains no signing, wallet, allowance, deposit, order-submission, or order-cancellation implementation.

## External adapters

### Polymarket

The Gamma adapter uses public search/list endpoints and validates their response with Pydantic models. Event descriptions are the audit source for rule parsing. The CLOB adapter sends token IDs to `POST /books` and validates each returned book. Numeric strings are converted to `Decimal` at the adapter boundary.

Public API uncertainty is isolated in these adapters. Unknown or malformed fields create provider errors or candidate rejections rather than guessed values.

The optional WebSocket connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribes by asset ID, sends `PING` every ten seconds, and accepts only validated public market events.

### Open-Meteo

The forecast adapter calls `/v1/ensemble` with explicit latitude, longitude, timezone, start date, end date, model list, and `temperature_2m`. It preserves model and member identity, hourly timestamps, retrieval time, and available initialization metadata.

Unavailable models are recorded independently; valid members from other configured models continue through the run. A run with fewer than the configured minimum valid members is rejected.

### Telegram

The Telegram adapter uses the HTTPS Bot API `sendMessage` method through the shared HTTP client. Missing Telegram credentials disable alerts without disabling scanning. Tokens are secret values and must not appear in logs or API responses.

## Rule normalization

The deterministic parser initially recognizes only daily maximum-temperature bucket events with sibling labels shaped as an exact degree, `N° or below`, or `N° or higher`. It parses the market date from explicit rule text, not from the current date. It parses a station identifier only when explicitly stated or encoded in the stated resolution URL.

Coordinates and timezone must come from `config/stations.yaml` or a market-specific override. Every configured station entry includes its metadata source. Overrides replace only named fields and are preserved as inferred/overridden field metadata.

Accepted rules store:

- market and event identifiers;
- location, coordinates, station, local date, and timezone;
- `daily_max_temperature`, source unit, resolution source, and reporting period;
- rounding method and precision;
- normalized bucket bounds and inclusive flags;
- original rules, confidence score, field provenance, and ambiguities.

Missing required fields or overlapping/gapped bucket definitions fail closed. Confidence is deterministic: required explicit fields carry fixed points, curated station metadata carries fixed points, and ambiguity deductions are fixed. A score below `MIN_RULE_CONFIDENCE` cannot create a signal.

## Probability and edge decisions

Temperature arithmetic uses floats because forecast precision exceeds market precision and no monetary arithmetic is involved. Money, prices, fees, balances, and share quantities use `Decimal`.

Each valid ensemble member contributes its configured model weight. Equal model weights are the default. Within a model, its weight is divided equally across valid members so a model with more members does not gain accidental influence. Missing models do not have their weight redistributed unless configuration explicitly permits it.

The pipeline computes:

`usable_edge = probability - best_ask - estimated_fee - slippage - uncertainty - rule_risk`

Every failed predicate is collected so one run records the complete rejection explanation. Alert fingerprints include market, outcome, price band, probability band, and threshold state. A new alert is allowed after a material change or cooldown expiry.

## Paper trading

The paper ledger starts at exactly $5.00. An entry uses the executable ask plus configured fee and slippage estimates. It must meet the CLOB minimum share size, fit within available cash, and not overlap an open position for the same market outcome.

Position creation and cash deduction occur in one database transaction. Manual settlement records payout and realized P&L. Automatic settlement and advanced performance slices are deferred until reliable resolved-market ingestion is implemented.

## Persistence

The first Alembic migration creates:

- `events`: Polymarket event metadata and original rules;
- `markets`: binary bucket markets, condition IDs, status, liquidity, close time, and minimum size;
- `outcomes`: labels, token IDs, and normalized bucket bounds;
- `order_book_snapshots`: full bid/ask JSON plus calculated top-of-book fields and depth;
- `normalized_rules`: normalized fields, provenance, confidence, ambiguity list, and original rules;
- `forecast_runs`: provider/model/run metadata and retrieval status;
- `forecast_members`: member identity, hourly series, daily value, bias, and quality state;
- `observations`: reserved persistence schema for the later official-observation milestone;
- `probability_estimates`: valid/excluded counts, probabilities, spread, uncertainty, and weights;
- `signals`: accepted decisions, price, probability, buffers, fingerprint, and alert state;
- `rejected_signals`: candidate and structured rejection reasons;
- `paper_positions`: entry details, shares, cost, fees, signal snapshot, and status;
- `paper_settlements`: resolution, payout, P&L, and scoring data;
- `provider_errors`: provider/operation errors and retry state with sanitized details;
- `application_settings`: persisted pause state and non-secret runtime settings.

UTC timestamps are stored internally. Market timezone and local date are separate fields. Foreign keys, uniqueness constraints, and indexes cover market/timestamp lookups, provider runs, open positions, and signal fingerprints.

## API and dashboard

The JSON API under `/api/v1` exposes read-only market, signal, position, performance-summary, provider-error, and configuration-status resources. `/health` proves the process is alive. `/ready` verifies configuration and a database query.

The server-rendered dashboard provides overview, market list/detail, signals, paper positions, provider errors, and configuration status. Forecast distributions, rules, books, probabilities, decisions, and data timestamps appear on market detail pages. Jinja autoescaping remains enabled.

## Reliability and security

One shared async HTTP client enforces timeouts, bounded exponential backoff with jitter, per-provider pacing, and simple circuit breakers. Retryable network and server failures are separated from permanent schema/validation failures. Secrets are redacted from structured JSON logs.

Scheduled jobs use `max_instances=1`, coalescing, and transaction boundaries. Provider failures do not terminate the process or erase prior valid state. Shutdown stops the scheduler, optional WebSocket, HTTP client, and database engine cleanly.

Environment settings are validated before startup. SQLite remains inside the persistent container volume and is not network-exposed. The container runs as an unprivileged user.

## File layout

Implementation uses focused modules rather than empty package layers:

```text
app/{main,config,database,models,schemas,logging,api,dashboard,worker}.py
app/services/{http,polymarket,rules,forecast,probability,signals,paper,telegram,websocket}.py
app/templates/{base,overview,market}.html
config/{stations,market_overrides}.yaml
tests/fixtures/london_temperature_event.json
tests/test_{rules,probability,signals,paper,providers,database,end_to_end}.py
alembic/versions/
Dockerfile
docker-compose.yml
pyproject.toml
.env.example
README.md
```

## Verification strategy

Core behavior is developed test-first. Tests cover units, local-day boundaries, daily maximum, rounding, bucket mapping, bias, model weighting, probabilities, fees, usable edge, ambiguity rejection, provider parsing, duplicate alerts, balance/minimum-size constraints, database persistence, and a recorded London event end-to-end flow.

External HTTP tests use recorded or constructed responses and no live dependency. The final gate runs pytest, Ruff, mypy, Alembic upgrade on a fresh database, application startup with safe configuration, Docker image build, and Docker Compose configuration validation.

## Deferred milestones

Official METAR/weather.gov observations, minimum temperature, rainfall, snowfall, hurricane/storm markets, Telegram commands, automatic resolution/settlement, calibration analytics, reverse-proxy automation, and any additional provider are added only after this complete temperature-bucket flow operates reliably.
