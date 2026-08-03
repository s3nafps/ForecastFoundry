from collections.abc import Mapping
from dataclasses import dataclass

from app.services.http import ResilientHttpClient


class GeoblockError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeoblockStatus:
    allowed: bool
    raw_data: Mapping[str, object]


async def check_geoblock(http: ResilientHttpClient, url: str) -> GeoblockStatus:
    try:
        payload = await http.request_json("GET", url)
    except Exception as exc:
        raise GeoblockError("geoblock check failed closed") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("blocked"), bool):
        raise GeoblockError("geoblock response is invalid")
    return GeoblockStatus(allowed=not payload["blocked"], raw_data=payload)


async def assert_geoblock_allows(http: ResilientHttpClient, url: str) -> GeoblockStatus:
    status = await check_geoblock(http, url)
    if not status.allowed:
        raise GeoblockError("geoblock reports this location is blocked")
    return status
