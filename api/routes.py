"""Health and authenticated, JavaScript-independent operator controls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from core.logging import redact_free_text
from core.models import KillSwitchState, utc_now
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from risk.kill_switch import TransitionNotRecorded

from api.controls import REARM_CHECKLIST, REASON_LIMIT, ControlResult, RearmRequest, parse_rearm
from api.dashboard import Status, build_dashboard, build_rearm_review
from api.operator import OperatorState

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
_SESSION_COOKIE = "operator_session"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_ROLE_LABELS = {"operator": "Operator", "admin": "Administrator"}


@router.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness only; never exposes broker or trading state."""

    return {"status": "ok"}


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
    return templates.TemplateResponse(
        request=request,
        name="operator_rearm.html",
        context={
            "page": _page(request, role),
            "review": build_rearm_review(state.to_dict()),
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
