from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Observation
from app.schemas import ForecastPoint
from app.services.http import ResilientHttpClient


class ObservationParseError(ValueError):
    pass


_FATAL_FLAGS = {"fatal", "invalid", "missing_temperature"}


@dataclass(frozen=True)
class ObservedHour:
    station_id: str
    observed_at: datetime
    temperature_celsius: float
    raw_ob: str
    quality_flags: tuple[str, ...]


def parse_aviation_weather_observations(
    payload: object, *, station_id: str
) -> tuple[ObservedHour, ...]:
    if not isinstance(payload, list):
        raise ObservationParseError("AviationWeather response must be a list")
    rows: list[ObservedHour] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise ObservationParseError("AviationWeather row must be an object")
        if str(raw.get("icaoId")) != station_id:
            raise ObservationParseError("AviationWeather station mismatch")
        try:
            observed_at = datetime.fromtimestamp(float(raw["obsTime"]), UTC)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ObservationParseError("AviationWeather obsTime is invalid") from exc
        flags = raw.get("quality_flags", [])
        if not isinstance(flags, list):
            raise ObservationParseError("AviationWeather quality_flags must be a list")
        quality = tuple(str(flag).lower() for flag in flags)
        if any(flag in _FATAL_FLAGS for flag in quality):
            continue
        try:
            temperature = float(raw["temp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObservationParseError("AviationWeather temp is invalid") from exc
        rows.append(
            ObservedHour(
                station_id=station_id,
                observed_at=observed_at,
                temperature_celsius=temperature,
                raw_ob=str(raw.get("rawOb") or ""),
                quality_flags=quality,
            )
        )
    return tuple(rows)


def apply_observations_to_points(
    points: Sequence[ForecastPoint],
    observations: Sequence[ObservedHour],
    *,
    now: datetime,
) -> tuple[ForecastPoint, ...]:
    """Replace forecast points at or before `now` with the latest observation
    for each point's hour window (no information from after `now`)."""
    past = tuple(obs for obs in observations if obs.observed_at <= now)
    if not past:
        return tuple(points)

    def latest_observed(timestamp: datetime) -> float | None:
        candidates = [obs for obs in past if obs.observed_at <= timestamp + timedelta(hours=1)]
        return (
            max(candidates, key=lambda obs: obs.observed_at).temperature_celsius
            if candidates
            else None
        )

    return tuple(
        ForecastPoint(
            timestamp=point.timestamp,
            temperature=(
                latest_observed(point.timestamp)
                if point.timestamp <= now and point.temperature is not None
                else point.temperature
            ),
        )
        for point in points
    )


async def load_day_observations(
    session: AsyncSession,
    *,
    station_id: str,
    source: str,
    local_date: object,
    timezone: str,
) -> tuple[Observation, ...]:
    rows = (
        await session.scalars(
            select(Observation)
            .where(Observation.station_id == station_id, Observation.source == source)
            .order_by(Observation.observed_at)
        )
    ).all()
    zone = ZoneInfo(timezone)
    return tuple(
        row
        for row in rows
        if row.air_temperature is not None
        and row.observed_at.astimezone(zone).date() == local_date
    )


async def ingest_observations(
    session: AsyncSession,
    *,
    station_id: str,
    source: str,
    rows: Sequence[ObservedHour],
    retrieved_at: datetime,
) -> int:
    inserted = 0
    for row in rows:
        exists = await session.scalar(
            select(Observation).where(
                Observation.station_id == station_id,
                Observation.observed_at == row.observed_at,
                Observation.source == source,
            )
        )
        if exists is not None:
            continue
        session.add(
            Observation(
                market_id=None,
                station_id=station_id,
                observed_at=row.observed_at,
                air_temperature=row.temperature_celsius,
                precipitation=None,
                source=source,
                retrieved_at=retrieved_at,
                quality_flags=list(row.quality_flags),
                raw_data={
                    "icaoId": row.station_id,
                    "obsTime": int(row.observed_at.timestamp()),
                    "temp": row.temperature_celsius,
                    "rawOb": row.raw_ob,
                },
            )
        )
        inserted += 1
    return inserted


class AviationWeatherObservations:
    def __init__(self, http: ResilientHttpClient, endpoint: str) -> None:
        self._http = http
        self._endpoint = endpoint

    async def fetch(self, station_id: str, local_date: object) -> tuple[ObservedHour, ...]:
        payload = await self._http.request_json(
            "GET",
            self._endpoint,
            params={
                "ids": station_id,
                "format": "json",
                "date": (
                    local_date.strftime("%Y%m%d")
                    if hasattr(local_date, "strftime")
                    else str(local_date)
                ),
            },
        )
        return parse_aviation_weather_observations(payload, station_id=station_id)
