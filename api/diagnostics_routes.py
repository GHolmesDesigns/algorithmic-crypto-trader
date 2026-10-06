"""``GET /operator/diagnostics``: the read-only report ``deploy/drill.sh diagnose`` prints.

Signed in as an operator, like ``/operator/state``. It changes nothing: a ``GET`` that reads
stored rows and asks the broker about pending orders. It answers 403 in ``live`` mode or with a
trade-capable credential scope, whoever asks, so the refusal does not depend on the script.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from api.diagnostics import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    REFUSAL,
    SqlAlchemyDiagnostics,
    build_diagnostics,
    permitted,
)
from api.routes import _authorize, _operator_state

router = APIRouter()

DIAGNOSTICS_PATH = "/operator/diagnostics"


@router.get(DIAGNOSTICS_PATH)
async def diagnostics(request: Request) -> Response:
    _authorize(request)
    state = _operator_state(request)
    if not permitted(state.settings.trading_mode, state.settings.credential_scope):
        raise HTTPException(status_code=403, detail=REFUSAL)
    limit = _limit(request)
    if limit is None:
        return JSONResponse(
            {"detail": f"only limit is accepted, a whole number from 1 to {MAX_LIMIT}"},
            status_code=422,
        )
    reader: SqlAlchemyDiagnostics | None = getattr(request.app.state, "diagnostics", None)
    increments = state.broker.capabilities.balance_increments if state.broker is not None else {}
    return JSONResponse(await build_diagnostics(state, reader, limit=limit, increments=increments))


def _limit(request: Request) -> int | None:
    """The requested count, the default when absent, or ``None`` for anything not accepted."""

    parameters = request.query_params
    if set(parameters) - {"limit"}:
        return None
    if "limit" not in parameters:
        return DEFAULT_LIMIT
    raw = parameters["limit"]
    if not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if 1 <= value <= MAX_LIMIT else None
