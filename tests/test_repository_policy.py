from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_repository_is_open_core_without_profit_or_custody_claims() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commercial = (ROOT / "docs" / "commercial.md").read_text(encoding="utf-8")

    assert "Apache License" in license_text
    assert "ForecastFoundry" in readme
    assert "does not promise returns" in readme
    assert "does not guarantee profit" in commercial
    assert "pool capital" in commercial
    assert "EXECUTION_ENABLED=false" in (ROOT / ".env.example").read_text(encoding="utf-8")
