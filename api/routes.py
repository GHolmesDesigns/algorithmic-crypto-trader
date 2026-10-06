"""Health and authenticated, JavaScript-independent operator controls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from core.logging import redact_free_text
from core.models import KillSwitchState, utc_now
from execution.closure import ClosableOrderStore, ClosureCode, ClosureOutcome, OrderCloser
from execution.engine import ExecutionEngine
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from risk.kill_switch import TransitionNotRecorded

from api.alerts import Alert
from api.controls import (
    REARM_CHECKLIST,
    REASON_LIMIT,
    ControlResult,
    RearmRequest,
    parse_close_order,
    parse_rearm,
)
from api.dashboard import Status, build_dashboard, build_rearm_review
from api.operator import OperatorState

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
_SESSION_COOKIE = "operator_session"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_ROLE_LABELS = {"operator": "Operator", "admin": "Administrator"}
FAVICON_FILE = Path(__file__).resolve().parent / "static" / "icons" / "bitcoin.ico"


@router.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness only; never exposes broker or trading state."""

    return {"status": "ok"}


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Unauthenticated, like /health: a fixed public image that reads no state."""

    return FileResponse(
        FAVICON_FILE,
        media_type="image/x-icon",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/health/detail")
async def detailed_health(request: Request) -> dict[str, object]:
    _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    return state.health()


@router.get("/health/strategies")
async def strategy_health(request: Request) -> dict[str, object]:
    _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    return {"strategies": state.health()["strategies"]}


@router.get("/operator/login", response_class=HTMLResponse)
async def operator_login(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request, name="operator_login.html", context={"page": _page(request, None)}
    )


@router.post("/operator/login")
async def operator_login_submit(request: Request) -> Response:
    body = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    supplied = body.get("token", [""])[0]
    role = _role_for_token(supplied)
    if role is None:
        raise HTTPException(status_code=401, detail="operator authentication required")
    if _wants_html(request):
        response: Response = RedirectResponse("/operator", status_code=303)
    else:
        response = JSONResponse({"status": "authenticated"})
    _set_session_cookie(response, role)
    return response


@router.post("/operator/logout")
async def operator_logout(request: Request) -> Response:
    """End the browser session. Works without a valid session, so it always clears."""

    session = _session_from_cookie(request.cookies.get(_SESSION_COOKIE))
    if session is not None:
        _revoke(request, session)
    result = ControlResult(
        action="sign_out",
        state="signed_out",
        changed=session is not None,
        at=utc_now(),
        role=session.role if session is not None else None,
    )
    response = _result_response(request, result, signed_in=False)
    response.delete_cookie(
        _SESSION_COOKIE,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("OPERATOR_COOKIE_SECURE", "0") == "1",
    )
    return response


@router.get("/operator", response_class=HTMLResponse)
async def operator_dashboard(request: Request) -> Response:
    return await _render_dashboard(request, "operator.html")


@router.get("/operator/fragment", response_class=HTMLResponse)
async def operator_fragment(request: Request) -> Response:
    return await _render_dashboard(request, "operator_fragment.html")


async def _render_dashboard(request: Request, template: str) -> Response:
    """Render the same ``/operator/state`` payload the JSON route returns."""

    role = _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    snapshot = state.to_dict()
    if role == "admin":
        _add_unresolved_orders(request, snapshot)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "snapshot": snapshot,
            "auth_role": role,
            "view": build_dashboard(snapshot, role=role),
        },
    )


@router.get("/operator/state")
async def operator_state(request: Request) -> dict[str, object]:
    _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    return state.to_dict()


@router.get("/operator/kill-switch")
async def kill_switch_status(request: Request) -> dict[str, str]:
    _authorize(request)
    return {"state": _operator_state(request).kill_switch.state.value}


@router.post("/operator/pause")
async def pause(request: Request) -> Response:
    return await _tighten_kill_switch(request, "pause", KillSwitchState.PAUSED, "operator pause")


@router.post("/operator/emergency-stop")
async def emergency_stop(request: Request) -> Response:
    return await _tighten_kill_switch(
        request, "emergency_stop", KillSwitchState.HALTED, "operator emergency stop"
    )


@router.get("/operator/rearm", response_class=HTMLResponse)
async def rearm_review(request: Request) -> Response:
    role = _authorize(request, required_role="admin")
    return await _render_rearm_review(request, role, RearmRequest((), ""), status_code=200)


@router.post("/operator/rearm")
async def rearm(request: Request) -> Response:
    """Lower the kill switch to running after the administrator review.

    Refuses, with the state unchanged, when any checklist item or the reference is
    missing (422) or the transition cannot be recorded (503).
    """

    role = _authorize(request, required_role="admin")
    submitted = parse_rearm(await request.body(), request.headers.get("content-type", ""))
    if submitted.errors:
        return await _refuse_rearm(request, role, submitted, status_code=422)
    switch = _operator_state(request).kill_switch
    previous = switch.state
    reason = redact_free_text(
        submitted.reason,
        secrets=(os.environ.get("OPERATOR_TOKEN", ""), os.environ.get("OPERATOR_ADMIN_TOKEN", "")),
    )
    try:
        changed = switch.rearm(actor=role, reason=reason, checklist=submitted.checklist)
    except TransitionNotRecorded:
        refused = RearmRequest(
            submitted.checklist,
            submitted.reason,
            ("The re-arm could not be recorded in the audit history, so it was not applied.",),
        )
        return await _refuse_rearm(request, role, refused, status_code=503)
    result = ControlResult(
        action="rearm",
        state=switch.state.value,
        changed=changed,
        at=utc_now(),
        role=role,
        previous=previous.value,
    )
    return _result_response(request, result)


_CLOSE_STATUS = {
    ClosureCode.CLOSED: 200,
    ClosureCode.INVALID_ID: 422,
    ClosureCode.ORDER_NOT_FOUND: 404,
    ClosureCode.NOT_UNRESOLVED: 409,
    ClosureCode.VENUE_HAS_ORDER: 409,
    ClosureCode.VENUE_HAS_FILLS: 409,
    ClosureCode.LOOKUP_FAILED: 502,
    ClosureCode.NOT_RECORDED: 503,
}


@router.post("/operator/orders/close")
async def close_order(request: Request) -> Response:
    """Close a pending or unknown order the venue never received, after asking it again.

    Administrator only. The venue is asked, under the trading lock, for the order by its client
    order ID and then for its fills; the order is closed only if it answers not-found and
    returns no fill. Refuses, with the order unchanged, when the request is incomplete (422),
    the order is missing (404), is not pending or unknown, or the venue has a record of it
    (409), a lookup fails (502), or the closure cannot be recorded (503). Never submits,
    cancels, or edits anything at the venue, and never touches the kill switch.
    """

    role = _authorize(request, required_role="admin")
    submitted = parse_close_order(await request.body(), request.headers.get("content-type", ""))
    reason = redact_free_text(
        submitted.reason,
        secrets=(os.environ.get("OPERATOR_TOKEN", ""), os.environ.get("OPERATOR_ADMIN_TOKEN", "")),
    )
    if submitted.errors:
        return _close_response(
            request, role, submitted.client_order_id, 422, errors=submitted.errors, reason=reason
        )
    closing = _order_closing(request)
    if closing is None:
        return _close_response(
            request,
            role,
            submitted.client_order_id,
            503,
            errors=("Closing orders is unavailable: no broker or order store is configured.",),
            reason=reason,
        )
    engine, store, lock = closing
    async with lock:
        outcome = await OrderCloser(engine.broker, store).close(
            submitted.client_order_id, actor=role, reason=reason
        )
        if outcome.closed:
            scheduler = _operator_state(request).scheduled_reconciliation
            if scheduler is not None:
                scheduler.forget_order(outcome.client_order_id)
    # Delivered after the lock is released: a slow alert destination must not stall trading.
    await _alert_close(request, outcome)
    return _close_response(
        request,
        role,
        submitted.client_order_id,
        _CLOSE_STATUS[outcome.code],
        outcome=outcome,
        reason=reason,
    )


def _order_closing(
    request: Request,
) -> tuple[ExecutionEngine, ClosableOrderStore, asyncio.Lock] | None:
    """The engine, its order store, and the trading lock, when this service can close orders."""

    engine = getattr(request.app.state, "execution", None)
    lock = getattr(request.app.state, "trading_lock", None)
    if not isinstance(engine, ExecutionEngine) or not isinstance(lock, asyncio.Lock):
        return None
    store = engine.store
    if not isinstance(store, ClosableOrderStore):
        return None
    return engine, store, lock


def _add_unresolved_orders(request: Request, snapshot: dict[str, Any]) -> None:
    """Add the saved pending or unknown orders to an administrator's snapshot.

    The snapshot's own order list is only what this process was handed, which the running
    service never is, so the close control and the re-arm review read the saved orders. With no
    order store there is nothing to add.
    """

    engine = getattr(request.app.state, "execution", None)
    if not isinstance(engine, ExecutionEngine):
        return
    try:
        orders = [order.model_dump(mode="json") for order in engine.store.pending()]
    except Exception:
        snapshot["unresolved_orders"] = {"readable": False, "orders": []}
        return
    snapshot["unresolved_orders"] = {"readable": True, "orders": orders}


async def _alert_close(request: Request, outcome: ClosureOutcome) -> None:
    """Tell the operator destinations about a close, and about a refused attempt."""

    short = outcome.client_order_id[:8]
    if outcome.closed:
        alert = Alert(
            condition="order_closed_never_received",
            severity="warning",
            message=(
                f"An administrator closed pending order {short}: the venue had no record of it "
                "and no fills. It was not resubmitted. The kill switch is unchanged."
            ),
        )
    else:
        alert = Alert(
            condition="order_close_refused",
            severity="warning",
            message=f"Closing order {short} was refused and nothing changed. {outcome.detail}",
        )
    try:
        await _operator_state(request).emit_alert(alert)
    except Exception:
        # The close already happened or was refused; an undeliverable alert is not a reason
        # to report otherwise. The dashboard's alert list still holds it.
        pass


def _close_response(
    request: Request,
    role: str,
    client_order_id: str,
    status_code: int,
    *,
    outcome: ClosureOutcome | None = None,
    errors: tuple[str, ...] = (),
    reason: str = "",
) -> Response:
    switch = _operator_state(request).kill_switch
    if not _wants_html(request):
        body: dict[str, Any] = {
            "closed": outcome is not None and outcome.closed,
            "client_order_id": client_order_id,
            "kill_switch": switch.state.value,
        }
        if outcome is not None:
            body.update(code=outcome.code.value, detail=outcome.detail)
            body["broker_lookup"] = outcome.broker_lookup
        else:
            body["errors"] = list(errors)
        return JSONResponse(body, status_code=status_code)
    return templates.TemplateResponse(
        request=request,
        name="operator_order_close.html",
        context={
            "page": _page(request, role),
            "outcome": outcome,
            "errors": errors,
            "client_order_id": client_order_id,
            "reason": reason,
            "at": utc_now(),
            "acting_role": _ROLE_LABELS.get(role, ""),
            "kill_switch": Status(
                switch.state.value,
                {"running": "ok", "paused": "warn", "halted": "crit"}.get(
                    switch.state.value, "neutral"
                ),
            ),
        },
        status_code=status_code,
    )


async def _tighten_kill_switch(
    request: Request, action: str, target: KillSwitchState, reason: str
) -> Response:
    """Operators can only raise severity; a stop never lowers a halt to a pause."""

    role = _authorize(request, required_role="operator")
    switch = _operator_state(request).kill_switch
    previous = switch.state
    changed = switch.tighten(target, reason=reason, actor=role)
    result = ControlResult(
        action=action,
        state=switch.state.value,
        changed=changed,
        at=utc_now(),
        role=role,
        previous=previous.value,
    )
    return _result_response(request, result)


async def _refuse_rearm(
    request: Request, role: str, submitted: RearmRequest, *, status_code: int
) -> Response:
    if _wants_html(request):
        return await _render_rearm_review(request, role, submitted, status_code=status_code)
    detail = {
        "errors": list(submitted.errors),
        "missing_checklist": list(submitted.missing),
        "state": _operator_state(request).kill_switch.state.value,
    }
    return JSONResponse({"detail": detail}, status_code=status_code)


async def _render_rearm_review(
    request: Request, role: str, submitted: RearmRequest, *, status_code: int
) -> Response:
    state = _operator_state(request)
    await state.refresh()
    snapshot = state.to_dict()
    _add_unresolved_orders(request, snapshot)
    return templates.TemplateResponse(
        request=request,
        name="operator_rearm.html",
        context={
            "page": _page(request, role),
            "review": build_rearm_review(snapshot),
            "checklist": REARM_CHECKLIST,
            "reason_limit": REASON_LIMIT,
            "submitted": submitted,
        },
        status_code=status_code,
    )


def _result_response(
    request: Request, result: ControlResult, *, signed_in: bool = True
) -> Response:
    if not _wants_html(request):
        return JSONResponse(result.to_dict())
    return templates.TemplateResponse(
        request=request,
        name="operator_result.html",
        context={
            "page": _page(request, result.role if signed_in else None),
            "result": result,
            "acting_role": _ROLE_LABELS.get(result.role or "", "None: no session was active"),
            "state_status": Status(result.state, result.tone),
        },
    )


def _page(request: Request, role: str | None) -> dict[str, object]:
    """Header facts for the result and review pages, without refreshing the broker."""

    settings = request.app.state.startup_settings
    mode = settings.trading_mode.value
    return {
        "mode": mode,
        "mode_tone": "crit" if mode == "live" else "accent",
        "credential_scope": settings.credential_scope.value,
        "role": _ROLE_LABELS.get(role or "", "Signed out"),
        "signed_in": role is not None,
    }


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


@dataclass(frozen=True, slots=True)
class _Session:
    role: str
    nonce: str
    expires_at: float


def _authorize(request: Request, required_role: str = "operator") -> str:
    """Return the caller's role from the token header or the session cookie."""

    operator_token = os.environ.get("OPERATOR_TOKEN")
    if not operator_token:
        raise HTTPException(status_code=503, detail="operator authentication is not configured")
    if "token" in request.query_params:
        # URLs end up in history, logs, and referrers. Scripts use the header.
        raise HTTPException(
            status_code=400,
            detail="tokens are not accepted in URLs; use the x-operator-token header or sign in",
        )

    supplied = request.headers.get("x-operator-token")
    if supplied:
        role = _role_for_token(supplied)
    else:
        session = _session_from_cookie(request.cookies.get(_SESSION_COOKIE))
        if session is not None and session.nonce in _revoked(request):
            session = None
        role = session.role if session is not None else None
    if role is None:
        raise HTTPException(status_code=401, detail="operator authentication required")
    if required_role == "admin" and role != "admin":
        raise HTTPException(status_code=403, detail="administrator authorization required")
    return role


def _role_for_token(supplied: str) -> str | None:
    if not supplied:
        return None
    admin_token = os.environ.get("OPERATOR_ADMIN_TOKEN")
    if admin_token and hmac.compare_digest(supplied, admin_token):
        return "admin"
    operator_token = os.environ.get("OPERATOR_TOKEN")
    if operator_token and hmac.compare_digest(supplied, operator_token):
        return "operator" if admin_token else "admin"
    return None


def _sign(message: str) -> str:
    secret = os.environ.get("OPERATOR_TOKEN", "").encode("utf-8")
    signature = hmac.new(secret, message.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def _set_session_cookie(response: Response, role: str) -> None:
    # The nonce lets sign-out revoke this session without touching any other.
    message = f"{role}.{int(time.time())}.{secrets.token_urlsafe(16)}"
    response.set_cookie(
        _SESSION_COOKIE,
        f"{message}.{_sign(message)}",
        max_age=_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("OPERATOR_COOKIE_SECURE", "0") == "1",
    )


def _session_from_cookie(value: str | None) -> _Session | None:
    if not value:
        return None
    try:
        role, issued, nonce, signature = value.split(".", 3)
        issued_at = int(issued)
    except ValueError:
        return None
    if role not in _ROLE_LABELS or abs(time.time() - issued_at) > _SESSION_TTL_SECONDS:
        return None
    if not hmac.compare_digest(signature, _sign(f"{role}.{issued}.{nonce}")):
        return None
    return _Session(role, nonce, issued_at + _SESSION_TTL_SECONDS)


def _revoked(request: Request) -> dict[str, float]:
    """Signed-out session nonces and when each would have expired anyway.

    Held in memory: a restart forgets them, and a signed-out browser has already
    dropped its cookie.
    """

    revoked = getattr(request.app.state, "revoked_sessions", None)
    if revoked is None:
        revoked = {}
        request.app.state.revoked_sessions = revoked
    return revoked


def _revoke(request: Request, session: _Session) -> None:
    revoked = _revoked(request)
    now = time.time()
    for nonce in [nonce for nonce, expires in revoked.items() if expires < now]:
        del revoked[nonce]
    revoked[session.nonce] = session.expires_at


def _operator_state(request: Request) -> OperatorState:
    try:
        return request.app.state.operator_state
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="operator state is not initialized") from exc
