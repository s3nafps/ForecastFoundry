import json
from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import ValidationError

from app.schemas import GammaEvent, GammaMarket, OrderBook, OrderLevel
from app.services.http import ProviderResponseError, ResilientHttpClient


def _string_list(value: object, field: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(f"Gamma {field} is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ProviderResponseError(f"Gamma {field} must be a string list")
    return tuple(decoded)


def _market(raw: Mapping[str, object]) -> GammaMarket:
    try:
        return GammaMarket(
            id=str(raw["id"]),
            question=str(raw["question"]),
            group_item_title=str(raw["groupItemTitle"]),
            condition_id=str(raw["conditionId"]),
            outcomes=_string_list(raw["outcomes"], "outcomes"),
            token_ids=_string_list(raw["clobTokenIds"], "clobTokenIds"),
            active=bool(raw.get("active")),
            closed=bool(raw.get("closed")),
            end_date=raw.get("endDate"),
            liquidity=raw.get("liquidityNum"),
            minimum_order_size=raw.get("orderMinSize"),
            description=str(raw.get("description") or ""),
            resolution_source=str(raw.get("resolutionSource") or ""),
            raw_data=dict(raw),
        )
    except (KeyError, ValidationError) as exc:
        raise ProviderResponseError("Gamma market response is incomplete") from exc


def parse_gamma_search(payload: object) -> tuple[GammaEvent, ...]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
        raise ProviderResponseError("Gamma search response must contain events")
    events: list[GammaEvent] = []
    for raw_event in payload["events"]:
        if not isinstance(raw_event, Mapping) or not isinstance(raw_event.get("markets"), list):
            raise ProviderResponseError("Gamma event response is incomplete")
        try:
            events.append(
                GammaEvent(
                    id=str(raw_event["id"]),
                    title=str(raw_event["title"]),
                    description=str(raw_event.get("description") or ""),
                    resolution_source=str(raw_event.get("resolutionSource") or ""),
                    active=bool(raw_event.get("active")),
                    closed=bool(raw_event.get("closed")),
                    end_date=raw_event.get("endDate"),
                    markets=tuple(_market(item) for item in raw_event["markets"]),
                    raw_data=dict(raw_event),
                )
            )
        except (KeyError, ValidationError) as exc:
            raise ProviderResponseError("Gamma event response is incomplete") from exc
    return tuple(events)


def parse_order_book(payload: object) -> OrderBook:
    if not isinstance(payload, Mapping):
        raise ProviderResponseError("CLOB order book must be an object")
    try:
        bids = tuple(
            sorted(
                (OrderLevel.model_validate(level) for level in payload["bids"]),
                key=lambda level: level.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (OrderLevel.model_validate(level) for level in payload["asks"]),
                key=lambda level: level.price,
            )
        )
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        spread = best_ask - best_bid if best_ask is not None and best_bid is not None else None
        midpoint = (
            (best_ask + best_bid) / 2 if best_ask is not None and best_bid is not None else None
        )
        return OrderBook(
            condition_id=str(payload["market"]),
            asset_id=str(payload["asset_id"]),
            timestamp=str(payload["timestamp"]),
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            midpoint=midpoint,
            available_depth=sum((level.size for level in asks), Decimal("0")),
            minimum_order_size=Decimal(str(payload["min_order_size"])),
            tick_size=Decimal(str(payload["tick_size"])),
            raw_data=dict(payload),
        )
    except (KeyError, TypeError, ValidationError, ValueError) as exc:
        raise ProviderResponseError("CLOB order-book response is invalid") from exc


class PolymarketClient:
    def __init__(self, http: ResilientHttpClient, gamma_url: str, clob_url: str) -> None:
        self._http = http
        self._gamma_url = gamma_url.rstrip("/")
        self._clob_url = clob_url.rstrip("/")

    async def discover_temperature_events(self) -> tuple[GammaEvent, ...]:
        payload = await self._http.request_json(
            "GET",
            f"{self._gamma_url}/public-search",
            params={
                "q": "highest temperature",
                "events_status": "active",
                "limit_per_type": 100,
                "search_profiles": "false",
            },
        )
        return parse_gamma_search(payload)

    async def discover_crypto_events(self) -> tuple[GammaEvent, ...]:
        payload = await self._http.request_json(
            "GET",
            f"{self._gamma_url}/public-search",
            params={
                "q": "Bitcoin Ethereum BTC ETH",
                "events_status": "active",
                "limit_per_type": 100,
                "search_profiles": "false",
            },
        )
        return parse_gamma_search(payload)

    async def get_order_books(self, token_ids: Sequence[str]) -> tuple[OrderBook, ...]:
        payload = await self._http.request_json(
            "POST",
            f"{self._clob_url}/books",
            json=[{"token_id": token_id} for token_id in token_ids],
        )
        if not isinstance(payload, list):
            raise ProviderResponseError("CLOB books response must be a list")
        return tuple(parse_order_book(book) for book in payload)
