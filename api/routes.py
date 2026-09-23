"""Health and authenticated, JavaScript-independent operator controls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs

from core.models import KillSwitchState
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from api.operator import OperatorState

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
_SESSION_COOKIE = "operator_session"
_SESSION_TTL_SECONDS = 8 * 60 * 60


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
    return templates.TemplateResponse(request=request, name="operator_login.html", context={})


@router.post("/operator/login")
async def operator_login_submit(request: Request) -> Response:
    body = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    supplied = body.get("token", [""])[0]
    role = _role_for_token(supplied)
    if role is None:
        raise HTTPException(status_code=401, detail="operator authentication required")
    if "text/html" in request.headers.get("accept", ""):
        response: Response = RedirectResponse("/operator", status_code=303)
    else:
        response = JSONResponse({"status": "authenticated"})
    _set_session_cookie(response, role)
    return response


@router.get("/operator", response_class=HTMLResponse)
async def operator_dashboard(request: Request) -> Response:
    auth = _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    response = templates.TemplateResponse(
        request=request,
        name="operator.html",
        context={"snapshot": state.to_dict(), "auth_role": auth.role},
    )
    if auth.from_token:
        _set_session_cookie(response, auth.role)
    return response


@router.get("/operator/fragment", response_class=HTMLResponse)
async def operator_fragment(request: Request) -> Response:
    auth = _authorize(request)
    state = _operator_state(request)
    await state.refresh()
    response = templates.TemplateResponse(
        request=request,
        name="operator_fragment.html",
        context={"snapshot": state.to_dict(), "auth_role": auth.role},
    )
    if auth.from_token:
        _set_session_cookie(response, auth.role)
    return response


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
    return await _set_kill_switch(request, KillSwitchState.PAUSED, "operator pause")


@router.post("/operator/emergency-stop")
async def emergency_stop(request: Request) -> Response:
    return await _set_kill_switch(request, KillSwitchState.HALTED, "operator emergency stop")


@router.post("/operator/rearm")
async def rearm(request: Request) -> Response:
    auth = _authorize(request, required_role="admin")
    state = _operator_state(request)
    state.kill_switch.set_state(KillSwitchState.RUNNING, reason="manual re-arm")
    return _control_response(request, state.kill_switch.state, auth)


async def _set_kill_switch(request: Request, target: KillSwitchState, reason: str) -> Response:
    auth = _authorize(request, required_role="operator")
    state = _operator_state(request)
    state.kill_switch.set_state(target, reason=reason)
    return _control_response(request, state.kill_switch.state, auth)


def _control_response(request: Request, state: KillSwitchState, auth: AuthContext) -> Response:
    if "text/html" in request.headers.get("accept", ""):
        body = (
            "<html><body><h1>Operator control applied</h1>"
            f"<p>Kill switch: <strong>{escape(state.value)}</strong></p>"
            '<p><a href="/operator">Return to dashboard</a></p></body></html>'
        )
        response: Response = HTMLResponse(body)
    else:
        response = JSONResponse({"state": state.value})
    if auth.from_token:
        _set_session_cookie(response, auth.role)
    return response


class AuthContext:
    def __init__(self, role: str, from_token: bool) -> None:
        self.role = role
        self.from_token = from_token


def _authorize(request: Request, required_role: str = "operator") -> AuthContext:
    operator_token = os.environ.get("OPERATOR_TOKEN")
    if not operator_token:
        raise HTTPException(status_code=503, detail="operator authentication is not configured")

    supplied = request.headers.get("x-operator-token") or request.query_params.get("token")
    if supplied:
        role = _role_for_token(supplied)
        if role is not None and _role_allows(role, required_role):
            return AuthContext(role, True)
        if role is not None and required_role == "admin":
            raise HTTPException(status_code=403, detail="administrator authorization required")
        raise HTTPException(status_code=401, detail="operator authentication required")
    session = _role_from_session(request.cookies.get(_SESSION_COOKIE))
    if session is not None and _role_allows(session, required_role):
        return AuthContext(session, False)
    if session is not None and required_role == "admin":
        raise HTTPException(status_code=403, detail="administrator authorization required")
    raise HTTPException(status_code=401, detail="operator authentication required")


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


def _role_allows(role: str, required_role: str) -> bool:
    return required_role == "operator" or role == "admin"


def _set_session_cookie(response: Response, role: str) -> None:
    issued = str(int(time.time()))
    message = f"{role}.{issued}".encode("ascii")
    secret = os.environ.get("OPERATOR_TOKEN", "").encode("utf-8")
    signature = hmac.new(secret, message, hashlib.sha256).digest()
    value = ".".join(
        (
            role,
            issued,
            base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        )
    )
    response.set_cookie(
        _SESSION_COOKIE,
        value,
        max_age=_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("OPERATOR_COOKIE_SECURE", "0") == "1",
    )


def _role_from_session(value: str | None) -> str | None:
    if not value:
        return None
    try:
        role, issued, encoded = value.split(".", 2)
        issued_at = int(issued)
        if role not in {"operator", "admin"} or abs(time.time() - issued_at) > _SESSION_TTL_SECONDS:
            return None
        padding = "=" * (-len(encoded) % 4)
        signature = base64.urlsafe_b64decode(encoded + padding)
    except (TypeError, ValueError):
        return None
    secret = os.environ.get("OPERATOR_TOKEN", "").encode("utf-8")
    message = f"{role}.{issued}".encode("ascii")
    expected = hmac.new(secret, message, hashlib.sha256).digest()
    return role if hmac.compare_digest(signature, expected) else None


def _operator_state(request: Request) -> OperatorState:
    try:
        return request.app.state.operator_state
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="operator state is not initialized") from exc
