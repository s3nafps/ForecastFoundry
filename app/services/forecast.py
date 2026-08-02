from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from app.schemas import ForecastMemberSeries, ForecastPoint, ForecastResult
from app.services.http import ProviderResponseError, ResilientHttpClient


class ForecastProvider(Protocol):
    async def get_forecast(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        timezone: str,
    ) -> ForecastResult: ...


def parse_open_meteo_response(
    payload: object, *, model: str, retrieved_at: datetime
) -> ForecastResult:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("hourly"), Mapping):
        raise ProviderResponseError("Open-Meteo response must contain hourly data")
    hourly = payload["hourly"]
    raw_times = hourly.get("time")
    timezone = payload.get("timezone")
    if not isinstance(raw_times, list) or not isinstance(timezone, str):
        raise ProviderResponseError("Open-Meteo timestamps or timezone are missing")
    zone = ZoneInfo(timezone)
    timestamps = tuple(
        datetime.fromisoformat(str(value)).replace(tzinfo=zone) for value in raw_times
    )
    members: list[ForecastMemberSeries] = []
    for key, raw_values in hourly.items():
        prefix = "temperature_2m_member"
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        if not isinstance(raw_values, list) or len(raw_values) != len(timestamps):
            raise ProviderResponseError(f"Open-Meteo member {key} has misaligned values")
        members.append(
            ForecastMemberSeries(
                member_id=key.removeprefix("temperature_2m_"),
                points=tuple(
                    ForecastPoint(timestamp=timestamp, temperature=value)
                    for timestamp, value in zip(timestamps, raw_values, strict=True)
                ),
            )
        )
    if not members:
        raise ProviderResponseError("Open-Meteo returned no individual ensemble members")
    try:
        return ForecastResult(
            provider="open_meteo",
            model=model,
            latitude=payload["latitude"],
            longitude=payload["longitude"],
            timezone=timezone,
            initialization_time=None,
            retrieved_at=retrieved_at,
            members=tuple(members),
            raw_metadata={key: value for key, value in payload.items() if key not in {"hourly"}},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderResponseError("Open-Meteo response metadata is invalid") from exc


class OpenMeteoProvider:
    def __init__(
        self,
        http: ResilientHttpClient,
        endpoint: str,
        model: str,
    ) -> None:
        self._http = http
        self._endpoint = endpoint
        self._model = model

    async def get_forecast(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        timezone: str,
    ) -> ForecastResult:
        payload = await self._http.request_json(
            "GET",
            self._endpoint,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m",
                "models": self._model,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "timezone": timezone,
            },
        )
        return parse_open_meteo_response(payload, model=self._model, retrieved_at=datetime.now(UTC))
