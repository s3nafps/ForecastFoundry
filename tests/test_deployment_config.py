from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_compose_separates_executor_and_mcp_secrets() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"app", "mcp"} <= set(services)
    assert services["app"]["user"] == "forecastfoundry"
    assert services["mcp"]["user"] == "forecastfoundry"
    assert services["app"]["env_file"]
    assert "env_file" not in services["mcp"]
    assert services["mcp"]["environment"]["EXECUTION_ENABLED"] == "false"
    assert not any("/data" in str(volume) for volume in services["mcp"].get("volumes", ()))
    assert services["app"]["healthcheck"]
    assert services["mcp"]["healthcheck"]


def test_docker_defaults_to_paper_and_loopback_http() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "EXECUTION_ENABLED=false" in env_example
    assert "REAL_TRADING_ENABLED=false" in env_example


def test_postgres_extra_declares_asyncpg() -> None:
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = config["project"]["optional-dependencies"]
    assert any("asyncpg" in extra for extra in extras["dev"])
