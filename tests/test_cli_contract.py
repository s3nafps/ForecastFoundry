import json

from app.cli import build_parser, main


def test_cli_exposes_agent_neutral_commands() -> None:
    parser = build_parser()

    for command in ("scan", "backtest", "status", "reconcile", "pause", "mcp"):
        parsed = parser.parse_args([command, "--json"])
        assert parsed.command == command
        assert parsed.json is True


def test_cli_returns_typed_idempotency_conflict(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(
        "FORECASTFOUNDRY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cli-control.db'}"
    )
    monkeypatch.setenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "operator-secret")
    assert (
        main(["resume", "--reason", "operator test", "--request-id", "cli-control-1", "--json"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(["pause", "--reason", "operator test", "--request-id", "cli-control-1", "--json"]) == 2
    )
    error = json.loads(capsys.readouterr().out)
    assert error["error_type"] == "idempotency_conflict"


def test_calibrate_command_is_registered() -> None:
    from app.cli import build_parser

    assert "calibrate" in build_parser().format_help()
