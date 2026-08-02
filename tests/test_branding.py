from pathlib import Path

from app import COMPATIBILITY_VERSION, PRODUCT_NAME
from app.config import Settings
from app.main import create_app


def test_public_product_identity_is_forecastfoundry() -> None:
    assert PRODUCT_NAME == "ForecastFoundry"
    assert COMPATIBILITY_VERSION == "weatheredge-v1"


def test_fastapi_title_and_config_keep_compatibility_storage(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'branding.db'}"
    settings = Settings(app_env="test", database_url=database_url)
    application = create_app(settings)

    assert application.title == "ForecastFoundry"
    assert settings.database_url == database_url
    assert Settings().database_url.endswith("data/weatheredge.db")
