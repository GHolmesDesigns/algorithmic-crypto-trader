"""The authenticated, JavaScript-independent Markets page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from api.markets import CandleQueryRefused, CandlesUnavailable
from api.markets_routes import _feed_states
from api.markets_view import MARKETS_PATH, build_markets_view, parse_markets_query
from api.routes import _authorize, _page, _wants_html, templates

router = APIRouter()


@router.get(MARKETS_PATH, response_class=HTMLResponse)
def markets_page(request: Request) -> Response:
    """Render one to nine stored-candle tiles, or a safe refusal/unavailable state."""

    role = _authorize(request)
    form = {key: value for key, value in request.query_params.multi_items()}
    reads = getattr(request.app.state, "market_candles", None)
    if reads is None:
        return _render(
            request,
            role,
            build_markets_view(
                None, form=form, unavailable="the candle database is not configured"
            ),
            503,
        )
    try:
        query = parse_markets_query(
            request.query_params.multi_items(),
            default_symbols=_watchlist(request),
        )
    except CandleQueryRefused as refused:
        return _render(
            request, role, build_markets_view(None, form=form, errors=refused.errors), 400
        )
    try:
        payload = reads.read(query, feed_states=_feed_states(request))
    except CandlesUnavailable as exc:
        return _render(
            request, role, build_markets_view(None, form=form, unavailable=str(exc)), 503
        )
    view = build_markets_view(payload, form=form)
    if not _wants_html(request):
        return JSONResponse({"kind": "markets", "status": "available", **payload})
    return _render(request, role, view, 200)


def _watchlist(request: Request) -> tuple[str, ...]:
    store = getattr(request.app.state, "watchlist", None)
    if store is None:
        return ()
    try:
        return tuple(store.symbols())
    except SQLAlchemyError:
        return ()


def _render(request: Request, role: str, view: dict[str, Any], status_code: int) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="operator_markets.html",
        context={"page": _page(request, role), "view": view},
        status_code=status_code,
    )
