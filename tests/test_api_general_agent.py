from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import make_engine
from app.main import create_app
from app.models import Base, DomainContract, Observation, ProviderError
from app.services.observations import ObservedHour

_FAKE_FETCH_CALLS: list[tuple[str, object]] = []


class _FakeAviationWeather:
    def __init__(self, http: object, endpoint: str) -> None:
        pass

    async def fetch(self, station_id: str, local_date: object) -> tuple[ObservedHour, ...]:
        _FAKE_FETCH_CALLS.append((station_id, local_date))
        return (
            ObservedHour(
                station_id=station_id,
                observed_at=datetime.now(UTC),
                temperature_celsius=12.5,
                raw_ob="EGLC test METAR",
                quality_flags=(),
            ),
        )


def _weather_contract(
    *,
    market_external_id: str,
    fingerprint: str,
    expiry: datetime,
    local_date: str,
    resolution_source: str = "aviation-weather-v1",
) -> DomainContract:
    return DomainContract(
        market_external_id=market_external_id,
        domain="weather",
        accepted=True,
        resolution_source=resolution_source,
        expiry=expiry,
        contract_data={"station_id": "EGLC", "local_date": local_date},
        rejection_reasons=[],
        provenance={},
        fingerprint=fingerprint,
    )


@pytest.mark.asyncio
async def test_general_agent_api_is_redacted_and_operator_controls_are_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'agent-api.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    monkeypatch.setenv("FORECASTFOUNDRY_OPERATOR_TOKEN", "operator-secret")
    app = create_app(Settings(app_env="test", database_url=database_url))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            domains = await client.get("/api/v1/domains")
            assert domains.json()["domains"] == ["weather", "crypto"]

            health = await client.get("/api/v1/providers/health")
            assert health.status_code == 200
            assert "operator-secret" not in health.text

            status = await client.get("/api/v1/execution/status")
            assert status.json()["live_execution"] is False
            assert status.json()["mode"] == "PAPER_ONLY"

            denied = await client.post(
                "/api/v1/operator/pause", json={"token": "wrong", "reason": "test"}
            )
            assert denied.status_code == 401
            paused = await client.post(
                "/api/v1/operator/pause",
                json={"token": "operator-secret", "reason": "operator test"},
            )
            assert paused.json()["paused"] is True
            assert (await client.get("/api/v1/execution/status")).json()["paused"] is True

            resumed = await client.post(
                "/api/v1/operator/resume",
                json={
                    "token": "operator-secret",
                    "reason": "operator test",
                    "request_id": "api-control-1",
                },
            )
            assert resumed.json()["paused"] is False
            conflict = await client.post(
                "/api/v1/operator/pause",
                json={
                    "token": "operator-secret",
                    "reason": "operator test",
                    "request_id": "api-control-1",
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["type"] == "idempotency_conflict"


async def test_observation_ingest_job_is_registered_on_app_state(
    tmp_path: Path,
) -> None:
    from app.main import create_app

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'observation-ingest.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    application = create_app(Settings(app_env="test", database_url=database_url))

    async with application.router.lifespan_context(application):
        assert hasattr(application.state, "run_observation_ingest")


async def test_observation_ingest_records_parse_errors_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'parse-abort.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    _FAKE_FETCH_CALLS.clear()
    monkeypatch.setattr("app.main.AviationWeatherObservations", _FakeAviationWeather)
    application = create_app(Settings(app_env="test", database_url=database_url))

    now = datetime.now(UTC)
    valid = _weather_contract(
        market_external_id="weather-eglc-valid",
        fingerprint="weather-eglc-valid",
        expiry=now + timedelta(hours=2),
        local_date=now.date().isoformat(),
    )
    malformed = _weather_contract(
        market_external_id="weather-eglc-malformed",
        fingerprint="weather-eglc-malformed",
        expiry=now + timedelta(hours=2),
        local_date="not-a-date",
        resolution_source="aviation-weather-v2",
    )

    async with application.router.lifespan_context(application):
        async with application.state.sessions() as session:
            session.add_all([valid, malformed])
            await session.commit()
        result = await application.state.run_observation_ingest()
        assert result == {"status": "completed", "ingested": 1, "errors": 1}
        assert _FAKE_FETCH_CALLS == [("EGLC", now.date())]
        async with application.state.sessions() as session:
            observations = (await session.scalars(select(Observation))).all()
            provider_errors = (await session.scalars(select(ProviderError))).all()
    assert len(observations) == 1
    assert observations[0].station_id == "EGLC"
    assert observations[0].source == "aviation-weather-v1"
    assert len(provider_errors) == 1
    assert provider_errors[0].provider == "aviation_weather"
    assert provider_errors[0].operation == "observe"
    assert provider_errors[0].error_type == "ValueError"


async def test_observation_ingest_skips_contracts_past_expiry_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'expired-grace.db'}"
    engine = make_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    _FAKE_FETCH_CALLS.clear()
    monkeypatch.setattr("app.main.AviationWeatherObservations", _FakeAviationWeather)
    application = create_app(Settings(app_env="test", database_url=database_url))

    expired = _weather_contract(
        market_external_id="weather-eglc-expired",
        fingerprint="weather-eglc-expired",
        expiry=datetime.now(UTC) - timedelta(hours=2),
        local_date=datetime.now(UTC).date().isoformat(),
    )

    async with application.router.lifespan_context(application):
        async with application.state.sessions() as session:
            session.add(expired)
            await session.commit()
        result = await application.state.run_observation_ingest()
        assert result == {"status": "completed", "ingested": 0, "errors": 0}
        assert _FAKE_FETCH_CALLS == []
        async with application.state.sessions() as session:
            observations = (await session.scalars(select(Observation))).all()
    assert observations == []
