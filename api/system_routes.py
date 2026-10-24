"""``GET /operator/system``: system health, incident detail and safe recovery guidance.

Signed in as an operator, like the Overview. The page only reads: the operator snapshot, the saved
unresolved orders, and the same bounded diagnostics report ``/operator/diagnostics`` returns. It
starts no recovery, writes nothing at a provider and never moves the kill switch. The controls it
links to (re-arm, close an order, EMERGENCY STOP, PAUSE) are the existing POST routes, each with
its own authorization and refusals.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from api.diagnostics import DEFAULT_LIMIT, REFUSAL, build_diagnostics, permitted
from api.operator import OperatorState
from api.routes import (
    _add_unresolved_orders,
    _authorize,
    _operator_state,
    _page,
    templates,
)
from api.system_view import build_system

logger = logging.getLogger(__name__)
router = APIRouter()

SYSTEM_PATH = "/operator/system"


@router.get(SYSTEM_PATH, response_class=HTMLResponse)
async def operator_system(request: Request) -> Response:
    role = _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    snapshot = state.to_dict()
    # Pending and unknown orders are visible to both roles; only the close control is admin-only.
    _add_unresolved_orders(request, snapshot)
    scheduler = state.scheduled_reconciliation
    view = build_system(
        snapshot,
        role=role,
        diagnostics=await _diagnostics(request, state),
        reconcile_interval=getattr(scheduler, "interval_seconds", None),
        persistence_configured=getattr(request.app.state, "diagnostics", None) is not None,
    )
    return templates.TemplateResponse(
        request=request,
        name="operator_system.html",
        context={"page": _page(request, role), "view": view},
    )


async def _diagnostics(request: Request, state: OperatorState) -> dict[str, Any]:
    """The bounded report, or the reason there is none. A failure never reaches the page as text."""

    if not permitted(state.settings.trading_mode, state.settings.credential_scope):
        return {"outcome": "refused", "reason": REFUSAL}
    reader = getattr(request.app.state, "diagnostics", None)
    try:
        increments = (
            state.broker.capabilities.balance_increments if state.broker is not None else {}
        )
        report = await build_diagnostics(state, reader, limit=DEFAULT_LIMIT, increments=increments)
    except Exception:
        logger.exception("the System page could not build the diagnostics report")
        return {"outcome": "failed"}
    return {"outcome": "report", "report": report}
