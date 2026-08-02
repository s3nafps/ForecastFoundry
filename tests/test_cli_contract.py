from app.cli import build_parser


def test_cli_exposes_agent_neutral_commands() -> None:
    parser = build_parser()

    for command in ("scan", "backtest", "status", "reconcile", "pause", "mcp"):
        parsed = parser.parse_args([command, "--json"])
        assert parsed.command == command
        assert parsed.json is True
