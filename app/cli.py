"""Thin command-line adapter over :mod:`app.services.application`."""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app import PRODUCT_NAME
from app.config import Settings
from app.database import make_engine, make_session_factory
from app.domains.base import MarketInput
from app.models import Base
from app.services.application import ApplicationServiceError, ApplicationServices
from app.services.execution_control import ExecutionControlConflict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forecastfoundry", description=PRODUCT_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="normalize and persist one market contract")
    scan.add_argument("--market-id")
    scan.add_argument("--title")
    scan.add_argument("--description", default="")
    scan.add_argument("--json", action="store_true")

    backtest = subparsers.add_parser("backtest", help="run a chronological paper backtest")
    backtest.add_argument("--dataset")
    backtest.add_argument("--json", action="store_true")

    calibrate = subparsers.add_parser(
        "calibrate", help="promote calibration-based model weights if the gate passes"
    )
    calibrate.add_argument("--min-samples", type=int, default=30)
    calibrate.add_argument("--min-improvement", type=float, default=0.05)
    calibrate.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="show persisted execution and paper status")
    status.add_argument("--json", action="store_true")

    portfolio = subparsers.add_parser("portfolio", help="show authoritative paper portfolio")
    portfolio.add_argument("--json", action="store_true")

    evidence = subparsers.add_parser("evidence", help="show immutable market evidence")
    evidence.add_argument("--market-id", required=True)
    evidence.add_argument("--json", action="store_true")

    explain = subparsers.add_parser("explain", help="explain the latest persisted prediction")
    explain.add_argument("--market-id", required=True)
    explain.add_argument("--json", action="store_true")

    reconcile = subparsers.add_parser("reconcile", help="reconcile persisted order state")
    reconcile.add_argument("--json", action="store_true")

    for name, help_text in (
        ("pause", "pause all new entries"),
        ("resume", "resume new entries after operator review"),
    ):
        control = subparsers.add_parser(name, help=help_text)
        control.add_argument("--reason", default="")
        control.add_argument("--request-id")
        control.add_argument("--expected-revision", type=int)
        control.add_argument("--json", action="store_true")

    mcp = subparsers.add_parser("mcp", help="run the MCP stdio server")
    mcp.add_argument("--json", action="store_true")
    return parser


async def _services() -> tuple[ApplicationServices, Any]:
    settings = Settings(
        database_url=os.getenv(
            "FORECASTFOUNDRY_DATABASE_URL", "sqlite+aiosqlite:///./data/weatheredge.db"
        ),
        app_env="cli",
    )
    engine = make_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return ApplicationServices(make_session_factory(engine), settings), engine


async def _execute(args: argparse.Namespace) -> dict[str, object]:
    services, engine = await _services()
    try:
        if args.command == "scan":
            if not args.market_id or not args.title:
                raise ApplicationServiceError("--market-id and --title are required for scan")
            return await services.scan_markets(
                [
                    MarketInput(
                        market_id=args.market_id,
                        title=args.title,
                        description=args.description,
                    )
                ]
            )
        if args.command == "backtest":
            if not args.dataset:
                raise ApplicationServiceError("--dataset is required for backtest")
            return await services.run_backtest(args.dataset)
        if args.command == "calibrate":
            return await services.run_calibration(
                min_samples=args.min_samples,
                min_improvement=Decimal(str(args.min_improvement)),
            )
        if args.command in {"status", "portfolio"}:
            return await services.portfolio_status()
        if args.command == "evidence":
            return await services.get_market_evidence(args.market_id)
        if args.command == "explain":
            return await services.explain_prediction(args.market_id)
        if args.command == "reconcile":
            return await services.reconcile_orders()
        if args.command in {"pause", "resume"}:
            return await services.set_execution(
                args.command == "pause",
                token=os.getenv("FORECASTFOUNDRY_OPERATOR_TOKEN", ""),
                reason=args.reason,
                actor="cli_operator",
                request_id=args.request_id,
                expected_revision=args.expected_revision,
            )
        raise ApplicationServiceError(f"unsupported command: {args.command}")
    finally:
        await engine.dispose()


def _print(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, default=str))
        return
    print(f"{PRODUCT_NAME}: {payload.get('status', payload.get('mode', 'ok'))}")
    for key, value in payload.items():
        if key not in {"status", "mode"}:
            print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        from app.mcp_server import main as mcp_main

        return mcp_main()
    try:
        payload = asyncio.run(_execute(args))
    except ExecutionControlConflict as exc:
        error: dict[str, object] = {
            "status": "error",
            "error": str(exc),
            "error_type": exc.code,
            "request_id": exc.request_id,
        }
        _print(error, as_json=args.json)
        return 2
    except (ApplicationServiceError, PermissionError, ValueError, OSError) as exc:
        validation_error: dict[str, object] = {"status": "error", "error": str(exc)}
        _print(validation_error, as_json=args.json)
        return 2
    _print(payload, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
