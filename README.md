# WeatherEdge

WeatherEdge is a paper-only intelligence service for active Polymarket daily maximum-temperature bucket markets. It discovers public markets, reads executable CLOB asks, turns Open-Meteo ensemble members into deterministic bucket probabilities, filters usable edge, records a $5 paper ledger, settles positions automatically when markets resolve, and can send Telegram alerts (signals and provider-outage alerts).

> **No real trading.** WeatherEdge v1 contains no wallet, private key, signing, order-placement, deposit, or withdrawal code. `REAL_TRADING_ENABLED=true` is rejected at startup. Forecast probability is an estimate, not truth or financial advice.

## Architecture and data

One FastAPI process serves the JSON API and escaped HTML dashboard, runs APScheduler polling jobs, and stores source and derived records in SQLite through async SQLAlchemy/Alembic. Polling is authoritative; an optional public Polymarket WebSocket only refreshes books sooner.

- Polymarket Gamma: public event discovery and original rules.
- Polymarket CLOB: public executable bids, asks, depth, tick size, and minimum order size.
- Open-Meteo Ensemble API: individual ensemble-member temperature series.
- Audited station registry: `config/stations.yaml`; initial coverage is London City Airport (`EGLC`).

The dashboard is at `/`; read-only JSON resources are under `/api/v1`. `/health` proves the process is alive and `/ready` verifies database access.

## Local installation

Python 3.12 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Linux/macOS, activate with `source .venv/bin/activate` and copy with `cp .env.example .env`.

Run quality checks with:

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy app
```

## Configuration

Settings are read from environment variables or `.env`; see `.env.example`. The important controls are polling intervals, automatic settlement (`SETTLEMENT_ENABLED`), the provider-error alert threshold (`PROVIDER_ERROR_ALERT_THRESHOLD`), rule confidence, minimum ensemble members, edge/spread/liquidity filters, buffers, paper balance, forecast models, and the optional WebSocket flag. Keep `REAL_TRADING_ENABLED=false`.

To enable Telegram alerts:

1. Message `@BotFather` in Telegram, create a bot, and place its token in `TELEGRAM_BOT_TOKEN`.
2. Send the new bot a message. Request `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your numeric chat ID into `TELEGRAM_ADMIN_USER_ID`.
3. Protect `.env`; it is ignored by Git and excluded from container builds.

Without both Telegram values, scanning and paper positions continue but no alert is sent.

## Docker and VPS deployment

Install Docker Engine with the Compose plugin, clone the repository, then:

```bash
cp .env.example .env
docker compose config
docker build -t weatheredge:test .
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
```

Compose publishes only the HTTP service on loopback, mounts the named `weatheredge-data` volume at `/data`, runs `alembic upgrade head` before Uvicorn, uses the unprivileged `weatheredge` user, checks `/health`, and restarts unless stopped.

For public access, install Nginx and a TLS certificate, replace the example hostname in `docs/reverse-proxy.conf`, copy it into `/etc/nginx/sites-enabled/`, test with `nginx -t`, then reload Nginx. Keep port 8000 bound to loopback; expose only Nginx ports 80/443 through the firewall.

View logs and upgrade with:

```bash
docker compose logs -f --tail=200 app
git pull --ff-only
docker compose build --pull
docker compose up -d
```

Stop the service without deleting its volume using `docker compose down`.

## Backup and restore

Stop the app before copying SQLite. The Compose project name fixes the volume name as `weatheredge_weatheredge-data`.

```bash
docker compose stop app
docker run --rm -v weatheredge_weatheredge-data:/data -v "$PWD:/backup" alpine \
  tar czf /backup/weatheredge-backup.tgz -C /data .
docker compose start app
```

Restore only after retaining the current volume as a rollback copy:

```bash
docker compose down
docker volume create weatheredge_rollback-data
docker run --rm -v weatheredge_weatheredge-data:/source -v weatheredge_rollback-data:/target alpine \
  sh -c 'cp -a /source/. /target/'
docker volume rm weatheredge_weatheredge-data
docker volume create weatheredge_weatheredge-data
docker run --rm -v weatheredge_weatheredge-data:/data -v "$PWD:/backup" alpine \
  tar xzf /backup/weatheredge-backup.tgz -C /data
docker compose up -d
```

## Troubleshooting

- `/health` fails: inspect `docker compose logs app`; confirm the container is running.
- `/ready` fails: check `/data` ownership, `DATABASE_URL`, free disk space, and migration errors.
- No markets: Gamma may have no matching active event, or its public schema changed; inspect `/api/v1/errors`.
- Markets are rejected: inspect rule confidence, station mapping, bucket completeness, liquidity, spread, minimum order size, and ensemble-member counts.
- No Telegram alert: verify both Telegram values and that the bot received an initial message from the target chat.
- SQLite is locked: run only one WeatherEdge app process against a database file and keep the database on a local Docker volume, not NFS.

## Known limitations

The initial milestone supports daily maximum-temperature buckets only, begins with the audited `EGLC` station, and uses configured Open-Meteo models with equal model weighting. Rule overrides are explicit YAML audit entries; ambiguous or incomplete rules are rejected. Station observations are not yet used to update temperature probabilities. Provider availability and upstream schemas can change, model members can be biased or correlated, and a high modeled probability does not guarantee resolution.
