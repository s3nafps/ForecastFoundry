from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.api import (
    config,
    error_rows,
    execution_status,
    market_detail,
    market_rows,
    performance_data,
    position_rows,
    research_rows,
    signal_rows,
)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/")
async def overview(request: Request) -> Response:
    markets = await market_rows(request)
    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={
            "title": "Overview",
            "markets": markets,
            "performance": await performance_data(request),
            "execution": await execution_status(request),
        },
    )


@router.get("/markets")
async def market_list(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="table.html",
        context={"title": "Active markets", "rows": await market_rows(request)},
    )


@router.get("/markets/{market_id}")
async def market_page(market_id: int, request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="market.html",
        context={"title": f"Market {market_id}", "market": await market_detail(market_id, request)},
    )


async def _table(request: Request, title: str, rows: list[dict[str, object]]) -> Response:
    return templates.TemplateResponse(
        request=request, name="table.html", context={"title": title, "rows": rows}
    )


@router.get("/signals")
async def signal_list(request: Request) -> Response:
    return await _table(request, "Signals", await signal_rows(request))


@router.get("/positions")
async def position_list(request: Request) -> Response:
    return await _table(request, "Paper positions", await position_rows(request))


@router.get("/performance")
async def performance_page(request: Request) -> Response:
    return await _table(request, "Performance", [await performance_data(request)])


@router.get("/errors")
async def error_list(request: Request) -> Response:
    return await _table(request, "Provider errors", await error_rows(request))


@router.get("/research")
async def research_page(request: Request) -> Response:
    return await _table(request, "Research evidence", await research_rows(request))


@router.get("/config")
async def config_page(request: Request) -> Response:
    return await _table(request, "Safe configuration", [await config(request)])
