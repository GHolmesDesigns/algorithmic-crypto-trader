"""The operator watchlist: coins to chart without trading them.

A signed-in operator can read the list and add, remove, or reorder coins, from
the form or as JSON. None of it touches trading: the watchlist is stored on its
own, and ``PAPER_SYMBOLS`` remains the only list the trading path uses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, urlencode

from data.feed_setting import ORIGIN_SAVED, FeedOrigin, FeedSettingError
from data.watchlist import (
    WATCHLIST_LIMIT,
    SqlAlchemyWatchlist,
    WatchlistRefused,
    check_product,
    normalize_symbol,
)
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from api.coin_catalog import CoinbaseCatalog
from api.routes import _authorize, _page, _wants_html, templates
from api.watchlist_view import WATCHLIST_PATH, build_watchlist_view

logger = logging.getLogger(__name__)
router = APIRouter()

_NOT_CONFIGURED = "the watchlist is not configured in this process"
_UNREADABLE = "the watchlist could not be read or saved"
_NOT_CONFIGURED_FEED = "the watch-only feed is not configured in this process"


class _Unavailable(RuntimeError):
    pass


@router.get(WATCHLIST_PATH)
def watchlist_page(request: Request) -> Response:
    role = _authorize(request)
    if not _wants_html(request) and request.query_params:
        return _refused(request, role, ("This view takes no parameters.",), 422)
    if set(request.query_params) - {"q", "added"}:
        return _refused(request, role, ("This view takes no parameters.",), 422)
    query = request.query_params.get("q", "")
    if len(query) > 80:
        return _refused(request, role, ("Search text must be 80 characters or fewer.",), 422)
    try:
        payload = _payload(request)
    except _Unavailable as exc:
        return _unavailable(request, role, str(exc))
    if not _wants_html(request):
        return JSONResponse(payload)
    catalog = _catalog_view(request, query, payload)
    added = request.query_params.get("added")
    try:
        added_symbol = normalize_symbol(added) if added else None
    except WatchlistRefused as exc:
        return _refused(request, role, (str(exc),), 422)
    view = build_watchlist_view(payload, role=role, catalog=catalog, added_symbol=added_symbol)
    return _render(request, role, view, 200)


@router.get(f"{WATCHLIST_PATH}/catalog")
def search_catalog(request: Request) -> Response:
    _authorize(request)
    if set(request.query_params) - {"q"}:
        return JSONResponse({"detail": "This view takes no parameters."}, 422)
    query = request.query_params.get("q", "")
    if len(query) > 80:
        return JSONResponse({"detail": "Search text must be 80 characters or fewer."}, 422)
    try:
        payload = _payload(request)
    except _Unavailable as exc:
        return JSONResponse({"detail": {"status": "unavailable", "reason": str(exc)}}, 503)
    return JSONResponse(_catalog_view(request, query, payload))


@router.post(f"{WATCHLIST_PATH}/catalog/refresh")
async def refresh_catalog(request: Request) -> Response:
    _authorize(request)
    catalog = getattr(request.app.state, "watch_catalog", None)
    if catalog is None:
        return JSONResponse(
            {"detail": {"status": "unavailable", "reason": "catalog is not configured"}},
            503,
        )
    await catalog.refresh()
    if not _wants_html(request):
        return JSONResponse(catalog.view())
    return RedirectResponse(WATCHLIST_PATH, status_code=303)


@router.post(f"{WATCHLIST_PATH}/add")
async def add_symbol(request: Request) -> Response:
    role = _authorize(request)
    form = await _body(request)
    raw = str(form.get("symbol", ""))
    query = str(form.get("q", ""))[:80]
    symbol: str | None = None
    try:
        symbol = normalize_symbol(raw)
        store = _store(request)
        current = store.symbols()
        if symbol in current:
            raise WatchlistRefused(f"{symbol} is already watched; the list is unchanged.")
        if len(current) >= WATCHLIST_LIMIT:
            raise WatchlistRefused(
                f"The watchlist holds at most {WATCHLIST_LIMIT} coins. Remove one first."
            )
        await check_product(request.app.state.watch_client, symbol)
        store.add(symbol, role=role)
    except WatchlistRefused as exc:
        return _refused(request, role, (str(exc),), 422, add_form={"symbol": raw, "q": query})
    except IntegrityError:
        try:
            already_saved = symbol is not None and symbol in _store(request).symbols()
        except SQLAlchemyError:
            already_saved = False
        if already_saved:
            return _refused(
                request,
                role,
                (f"{symbol} was added by another request; the list is unchanged.",),
                422,
                add_form={"symbol": raw, "q": query},
            )
        return _unavailable(request, role, _UNREADABLE)
    except (_Unavailable, SQLAlchemyError) as exc:
        return _unavailable(request, role, _reason(exc))
    feed = getattr(request.app.state, "watch_feed", None)
    if feed is not None:
        feed.wake()
    return _done(request, "added", symbol)


@router.post(f"{WATCHLIST_PATH}/remove")
async def remove_symbol(request: Request) -> Response:
    role = _authorize(request)
    form = await _body(request)
    try:
        symbol = normalize_symbol(str(form.get("symbol", "")))
        _store(request).remove(symbol, role=role)
    except WatchlistRefused as exc:
        return _refused(request, role, (str(exc),), 422)
    except (_Unavailable, SQLAlchemyError) as exc:
        return _unavailable(request, role, _reason(exc))
    return _done(request, "removed", symbol)


@router.post(f"{WATCHLIST_PATH}/reorder")
async def reorder_symbols(request: Request) -> Response:
    """Set the whole order (``order``), or move one coin a step (``symbol`` and ``direction``)."""

    role = _authorize(request)
    form = await _body(request)
    try:
        store = _store(request)
        current = list(store.symbols())
        if "order" in form:
            order = form["order"]
            wanted = (
                [str(item) for item in order]
                if isinstance(order, list)
                else [item for item in str(order).split(",") if item.strip()]
            )
        else:
            symbol = normalize_symbol(str(form.get("symbol", "")))
            direction = str(form.get("direction", ""))
            if symbol not in current or direction not in {"up", "down"}:
                raise WatchlistRefused("Choose a watched coin and up or down.")
            index = current.index(symbol)
            target = index - 1 if direction == "up" else index + 1
            wanted = current
            if 0 <= target < len(current):
                wanted[index], wanted[target] = wanted[target], wanted[index]
        result = store.reorder(wanted, role=role)
    except WatchlistRefused as exc:
        return _refused(request, role, (str(exc),), 422)
    except (_Unavailable, SQLAlchemyError) as exc:
        return _unavailable(request, role, _reason(exc))
    if not _wants_html(request):
        return JSONResponse({"status": "reordered", "order": list(result)})
    return RedirectResponse(WATCHLIST_PATH, status_code=303)


@router.post(f"{WATCHLIST_PATH}/feed")
async def set_feed(request: Request) -> Response:
    """Turn the watch-only feed on or off; an administrator only, saved across restarts."""

    role = _authorize(request, "admin")
    form = await _body(request)
    wanted = _parse_enabled(form.get("enabled"))
    if wanted is None:
        return _refused(request, role, ("Choose to turn the feed on or off.",), 422)
    controller = getattr(request.app.state, "watch_feed_controller", None)
    setting = getattr(request.app.state, "watch_feed_setting", None)
    if controller is None or setting is None:
        return _unavailable(request, role, _NOT_CONFIGURED_FEED)
    was = controller.running
    if wanted == was:
        return _feed_done(request, "unchanged", wanted)
    try:
        if wanted:
            controller.start()
        try:
            choice = setting.save(wanted, previous=was, role=role)
        except FeedSettingError:
            if wanted:
                await controller.stop()
            raise
        if not wanted:
            await controller.stop()
    except FeedSettingError as exc:
        return _unavailable(request, role, str(exc))
    except Exception:
        logger.exception("watch-only feed could not be turned %s", "on" if wanted else "off")
        return _unavailable(request, role, "the watch-only feed could not be changed")
    controller.origin = FeedOrigin(ORIGIN_SAVED, choice.changed_by, choice.changed_at)
    return _feed_done(request, "turned_on" if wanted else "turned_off", wanted)


def _store(request: Request) -> SqlAlchemyWatchlist:
    store = getattr(request.app.state, "watchlist", None)
    if store is None:
        raise _Unavailable(_NOT_CONFIGURED)
    return store


def _catalog(request: Request) -> CoinbaseCatalog:
    catalog = getattr(request.app.state, "watch_catalog", None)
    if catalog is None:
        catalog = CoinbaseCatalog(None)
    return catalog


def _catalog_view(request: Request, query: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = _catalog(request).view(query)
    watched = {item["symbol"] for item in payload["symbols"]}
    matches = [
        {**item, "already_watched": item["product_id"] in watched} for item in result["matches"]
    ]
    return {
        **result,
        "has_selectable_match": any(not item["already_watched"] for item in matches),
        "matches": matches,
    }


def _reason(exc: Exception) -> str:
    return _NOT_CONFIGURED if isinstance(exc, _Unavailable) else _UNREADABLE


def _payload(request: Request) -> dict[str, Any]:
    try:
        entries = _store(request).entries()
    except SQLAlchemyError as exc:
        raise _Unavailable(_UNREADABLE) from exc
    feed = getattr(request.app.state, "watch_feed", None)
    feed_state: dict[str, Any] = (
        feed.to_dict() if feed is not None else {"enabled": False, "symbols": []}
    )
    states = {item["symbol"]: item for item in feed_state["symbols"]}
    trading = sorted(feed.trading_symbols) if feed is not None else []
    controller = getattr(request.app.state, "watch_feed_controller", None)
    return {
        "limit": WATCHLIST_LIMIT,
        "feed_enabled": feed is not None,
        "feed_setting": controller.to_dict() if controller is not None else None,
        "trading_symbols": trading,
        "symbols": [
            {
                "symbol": entry.symbol,
                "position": entry.position,
                "added_at": entry.added_at.isoformat(),
                "added_by": entry.added_by,
                "collected_by": _collector(entry.symbol, trading, feed is not None),
                "feed": states.get(entry.symbol),
            }
            for entry in entries
        ],
    }


def _collector(symbol: str, trading: Sequence[str], feed_on: bool) -> str | None:
    if symbol in trading:
        return "trading feed"
    return "watch-only feed" if feed_on else None


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _parse_enabled(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "on"}:
        return True
    if text in {"0", "false", "off"}:
        return False
    return None


def _feed_done(request: Request, status: str, enabled: bool) -> Response:
    if not _wants_html(request):
        return JSONResponse({"status": status, "enabled": enabled})
    return RedirectResponse(WATCHLIST_PATH, status_code=303)


def _done(request: Request, status: str, symbol: str) -> Response:
    if not _wants_html(request):
        return JSONResponse({"status": status, "symbol": symbol})
    if status == "added":
        query = urlencode({"added": symbol})
        return RedirectResponse(f"{WATCHLIST_PATH}?{query}#added-coin", status_code=303)
    return RedirectResponse(WATCHLIST_PATH, status_code=303)


def _refused(
    request: Request,
    role: str,
    errors: Sequence[str],
    status_code: int,
    *,
    add_form: dict[str, str] | None = None,
) -> Response:
    if not _wants_html(request):
        return JSONResponse({"detail": {"status": "refused", "errors": list(errors)}}, status_code)
    try:
        payload = _payload(request)
    except _Unavailable as exc:
        return _unavailable(request, role, str(exc))
    query = (add_form or {}).get("q", "")
    view = build_watchlist_view(
        payload,
        role=role,
        errors=errors,
        add_form=add_form,
        catalog=_catalog_view(request, query, payload),
    )
    return _render(request, role, view, status_code)


def _unavailable(request: Request, role: str, reason: str) -> Response:
    if not _wants_html(request):
        return JSONResponse({"detail": {"status": "unavailable", "reason": reason}}, 503)
    view = build_watchlist_view(None, role=role, unavailable=reason)
    return _render(request, role, view, 503)


def _render(request: Request, role: str, view: dict[str, Any], status_code: int) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="operator_watchlist.html",
        context={"page": _page(request, role), "view": view},
        status_code=status_code,
    )
