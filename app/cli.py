import argparse
import json
from collections.abc import Sequence

from app import PRODUCT_NAME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forecastfoundry", description=PRODUCT_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "backtest", "status", "reconcile", "pause", "mcp"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        from app.mcp_server import main as mcp_main

        return mcp_main()
    payload = {"product_name": PRODUCT_NAME, "command": args.command, "mode": "PAPER_ONLY"}
    output = (
        json.dumps(payload)
        if args.json
        else f"{PRODUCT_NAME}: {args.command} ({payload['mode']})"
    )
    print(output)
    return 0
