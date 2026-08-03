# ForecastFoundry paper baseline

This is a reproducibility record, not a performance claim. On 2026-08-03 the
local Python 3.12 test gate completed with 88 passing tests and Ruff reported
no violations. The deterministic crypto model tests cover seeded bootstrap,
EWMA volatility, Monte Carlo bounds, input hashes, stale/duplicate candle
rejection, Brier score, and reliability buckets. The recorded fixture tests
cover strict BTC/ETH contract parsing, weather routing, paper ledger behavior,
geoblock failure, encrypted keystore validation, risk caps, reconciliation,
MCP tool filtering, REST redaction, and deployment secret separation.

No live order endpoint, signer, wallet transfer, or customer funds were used.
The default `.env.example`, Compose executor, MCP service, CLI, and tests keep
`EXECUTION_ENABLED=false` or paper-only behavior. A real trading deployment
still requires a fresh walk-forward calibration report, source-license review,
geoblock/jurisdiction review, operator approval, and a disposable-account
smoke test.
