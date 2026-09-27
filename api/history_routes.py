"""Authenticated, read-only history routes: JSON for scripts, HTML for browsers.

Every route is a GET that reads persisted rows through ``SqlAlchemyHistory``, or
counts them per time bucket through ``SqlAlchemyTrends``.
None can submit, cancel, or retry an order. A request asking for more than one
bounded page or window is refused with 422 before anything is read. A history
that cannot be read answers 503, which is never shown as an empty history.

Handlers are plain functions, so FastAPI runs the database reads in its thread
pool rather than on the event loop the trading runtime shares.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from api.history import (
    DEFAULT_LIMIT,
    KILL_SWITCH_EVENT,
    HistoryQuery,
    HistoryQueryRefused,
    HistoryUnavailable,
    SqlAlchemyHistory,
    parse_query,
)
from api.history_view import PATHS, build_history_view, build_lineage_view
from api.routes import _authorize, _operator_state, _page, _wants_html, templates
from api.trends import SqlAlchemyTrends, not_started, parse_trends_query
from api.trends_view import build_trends_view

router = APIRouter()

_TEMPLATES = {"lineage": "operator_lineage.html", "trends": "operator_trends.html"}
_NOT_CONFIGURED = "the history database is not configured in this process"
_UNREADABLE = "the history database could not be read"


@router.get(PATHS["orders"])
def order_history(request: Request) -> Response:
    return _list(request, "orders")


@router.get(PATHS["signals"])
def signal_history(request: Request) -> Response:
    return _list(request, "signals")


@router.get(PATHS["risk_decisions"])
def risk_decision_history(request: Request) -> Response:
    return _list(request, "risk_decisions")


@router.get(PATHS["discrepancies"])
def discrepancy_history(request: Request) -> Response:
    return _list(request, "discrepancies")


@router.get(PATHS["events"])
def event_history(request: Request) -> Response:
    return _list(request, "events")


@router.get(PATHS["risk"])
def risk_history(request: Request) -> Response:
    """Refusals by gate, the latest refusal, and kill-switch history for one window."""

    role = _authorize(request)
    form = dict(request.query_params)
    try:
        query = parse_query("refusals", request.query_params.multi_items())
    except HistoryQueryRefused as refused:
        return _refused(request, role, "risk", form, refused.errors)
    try:
        history = _history(request)
        refusals = history.refusals(query)
        transitions = history.events(
            HistoryQuery(
                "events",
                query.since,
                query.until,
                limit=DEFAULT_LIMIT,
                window=query.window,
                filters={"event_type": KILL_SWITCH_EVENT},
            )
        )
    except HistoryUnavailable as exc:
        return _unavailable(request, role, "risk", form, str(exc))
    payload = {
        "kind": "risk",
        "status": "available",
        "query": query.to_dict(),
        "refusals": refusals,
        "kill_switch": {
            "state": _operator_state(request).kill_switch.state.value,
            "total": transitions.total,
            "rows": transitions.rows,
            "next_before": transitions.next_before,
        },
    }
    return _respond(request, role, "risk", form, payload, query)


@router.get(PATHS["trends"])
def trends(request: Request) -> Response:
    """Counts per time bucket over one bounded window, and the charts with no producer yet."""

    role = _authorize(request)
    form = dict(request.query_params)
    try:
        query = parse_trends_query(request.query_params.multi_items())
    except HistoryQueryRefused as refused:
        if not _wants_html(request):
            return JSONResponse(
                {"detail": {"status": "refused", "errors": list(refused.errors)}}, 422
            )
        return _render(request, role, build_trends_view(form=form, errors=refused.errors), 422)
    try:
        charts = _trends(request).read(query)
    except HistoryUnavailable as exc:
        reason = _NOT_CONFIGURED if str(exc) == _NOT_CONFIGURED else _UNREADABLE
        if not _wants_html(request):
            return JSONResponse({"detail": {"status": "unavailable", "reason": reason}}, 503)
        view = build_trends_view(
            form=form, query=query, not_started=not_started(), unavailable=reason
        )
        return _render(request, role, view, 503)
    if not _wants_html(request):
        return JSONResponse(
            {
                "kind": "trends",
                "status": "available",
                "query": query.to_dict(),
                "charts": charts,
            }
        )
    view = build_trends_view(
        form=form,
        query=query,
        charts=[chart for chart in charts if chart["status"] == "available"],
        not_started=[chart for chart in charts if chart["status"] == "not_started"],
    )
    return _render(request, role, view, 200)


@router.get(PATHS["orders"] + "/{client_order_id}")
def order_lineage(request: Request, client_order_id: str) -> Response:
    """One order's signal, risk decision, and fills, rebuilt from persisted rows."""

    role = _authorize(request)
    if request.query_params:
        return _lineage_response(
            request, role, client_order_id, None, 422, "This view takes no parameters."
        )
    try:
        key = UUID(client_order_id)
    except ValueError:
        return _lineage_response(
            request, role, client_order_id, None, 422, "The client order ID must be a UUID."
        )
    try:
        row = _history(request).lineage(key)
    except HistoryUnavailable as exc:
        return _lineage_response(request, role, client_order_id, None, 503, str(exc))
    if row is None:
        return _lineage_response(
            request, role, client_order_id, None, 404, "No order with this client order ID."
        )
    return _lineage_response(request, role, str(key), row, 200, None)


def _list(request: Request, kind: str) -> Response:
    role = _authorize(request)
    form = dict(request.query_params)
    try:
        query = parse_query(kind, request.query_params.multi_items())
    except HistoryQueryRefused as refused:
        return _refused(request, role, kind, form, refused.errors)
    try:
        page = getattr(_history(request), kind)(query)
    except HistoryUnavailable as exc:
        return _unavailable(request, role, kind, form, str(exc))
    payload = {
        "kind": kind,
        "status": "available",
        "query": query.to_dict(),
        "total": page.total,
        "count": len(page.rows),
        "rows": page.rows,
        "next_before": page.next_before,
    }
    return _respond(request, role, kind, form, payload, query)


def _history(request: Request) -> SqlAlchemyHistory:
    history = getattr(request.app.state, "history", None)
    if history is None:
        raise HistoryUnavailable(_NOT_CONFIGURED)
    return history


def _trends(request: Request) -> SqlAlchemyTrends:
    trends = getattr(request.app.state, "trends", None)
    if trends is None:
        raise HistoryUnavailable(_NOT_CONFIGURED)
    return trends


def _respond(
    request: Request,
    role: str,
    kind: str,
    form: dict[str, str],
    payload: dict[str, Any],
    query: HistoryQuery,
) -> Response:
    if not _wants_html(request):
        return JSONResponse(payload)
    view = build_history_view(kind, form=form, query=query, payload=payload)
    return _render(request, role, view, 200)


def _refused(
    request: Request, role: str, kind: str, form: dict[str, str], errors: tuple[str, ...]
) -> Response:
    if not _wants_html(request):
        return JSONResponse({"detail": {"status": "refused", "errors": list(errors)}}, 422)
    view = build_history_view(kind, form=form, errors=errors)
    return _render(request, role, view, 422)


def _unavailable(
    request: Request, role: str, kind: str, form: dict[str, str], detail: str
) -> Response:
    reason = _NOT_CONFIGURED if detail == _NOT_CONFIGURED else _UNREADABLE
    if not _wants_html(request):
        return JSONResponse({"detail": {"status": "unavailable", "reason": reason}}, 503)
    view = build_history_view(kind, form=form, unavailable=reason)
    return _render(request, role, view, 503)


def _lineage_response(
    request: Request,
    role: str,
    client_order_id: str,
    row: dict[str, Any] | None,
    status_code: int,
    problem: str | None,
) -> Response:
    reason = None
    if status_code == 503:
        reason = _NOT_CONFIGURED if problem == _NOT_CONFIGURED else _UNREADABLE
    if not _wants_html(request):
        if row is not None:
            return JSONResponse({"kind": "lineage", "status": "available", "lineage": row})
        status = {503: "unavailable", 404: "not_recorded"}.get(status_code, "refused")
        return JSONResponse(
            {"detail": {"status": status, "reason": reason or problem}}, status_code
        )
    view = build_lineage_view(
        row,
        client_order_id=client_order_id[:64],
        unavailable=reason,
        problem=problem if status_code in {404, 422} else None,
    )
    return _render(request, role, view, status_code)


def _render(request: Request, role: str, view: dict[str, Any], status_code: int) -> Response:
    template = _TEMPLATES.get(view["kind"], "operator_history.html")
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"page": _page(request, role), "view": view},
        status_code=status_code,
    )
