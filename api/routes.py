"""Health and authenticated, JavaScript-independent operator controls."""

from __future__ import annotations

import os

from core.models import KillSwitchState
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _kill_switch(request: Request):
    try:
        return request.app.state.kill_switch
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="kill switch is not initialized") from exc


def _authorize(request: Request) -> None:
    expected = os.environ.get("OPERATOR_TOKEN")
    supplied = request.headers.get("x-operator-token") or request.query_params.get("token")
    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="operator authentication required")


@router.get("/operator", response_class=HTMLResponse)
async def operator_dashboard(request: Request) -> HTMLResponse:
    _authorize(request)
    state = _kill_switch(request).state.value
    token = request.query_params.get("token", "")
    return HTMLResponse(
        "<html><body><h1>Trading operator controls</h1>"
        f"<p>Kill switch: <strong>{state}</strong></p>"
        f'<form method="post" action="/operator/pause?token={token}">'
        '<button type="submit">PAUSE</button></form>'
        f'<form method="post" action="/operator/emergency-stop?token={token}">'
        '<button type="submit">EMERGENCY STOP</button></form>'
        f'<form method="post" action="/operator/rearm?token={token}">'
        '<button type="submit">RE-ARM</button></form>'
        "</body></html>"
    )


@router.get("/operator/kill-switch")
async def kill_switch_status(request: Request) -> dict[str, str]:
    _authorize(request)
    return {"state": _kill_switch(request).state.value}


@router.post("/operator/pause")
async def pause(request: Request) -> dict[str, str]:
    _authorize(request)
    switch = _kill_switch(request)
    switch.set_state(KillSwitchState.PAUSED, reason="operator pause")
    return {"state": switch.state.value}


@router.post("/operator/emergency-stop")
async def emergency_stop(request: Request) -> dict[str, str]:
    _authorize(request)
    switch = _kill_switch(request)
    switch.set_state(KillSwitchState.HALTED, reason="operator emergency stop")
    return {"state": switch.state.value}


@router.post("/operator/rearm")
async def rearm(request: Request) -> dict[str, str]:
    _authorize(request)
    switch = _kill_switch(request)
    switch.set_state(KillSwitchState.RUNNING, reason="manual re-arm")
    return {"state": switch.state.value}
