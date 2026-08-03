# Operator runbook

1. Run the one-shot migration service and confirm `/ready`.
2. Keep `EXECUTION_ENABLED=false` until source health, calibration, geoblock,
   jurisdiction, caps, and reconciliation have been reviewed.
3. Use the CLI or REST `Authorization: Bearer` control to pause before
   maintenance; include a unique request ID for automation.
4. Investigate `provider_health`, rejected candidates, `/metrics`, and
   `/api/v1/orders/reconciliation` before resuming.
5. Unknown or partial provider order state is a hard stop. Reconcile manually;
   do not blindly retry.
