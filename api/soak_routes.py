"""The Soak & readiness console: a read-only GET for anyone signed in, and one
administrator-only POST that records a criterion's evidence.

The GET never takes a parameter: it always shows the last 30 UTC days. The POST
is the only write path in this module, and ``_authorize`` refuses it below the
administrator role before ``parse_evidence`` even looks at the body.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from api.history import HistoryUnavailable
from api.routes import _authorize, _page, _wants_html, templates
from api.soak import SqlAlchemySoak, parse_evidence
from api.soak_view import EVIDENCE_PATH, SOAK_PATH, build_soak_view

router = APIRouter()

_NOT_CONFIGURED = "the soak console is not configured in this process"
_UNREADABLE = "the soak console could not be read"


@router.get(SOAK_PATH)
def soak_console(request: Request) -> Response:
    role = _authorize(request)
    if request.query_params:
        errors = ("This view takes no parameters.",)
        if not _wants_html(request):
            return JSONResponse({"detail": {"status": "refused", "errors": list(errors)}}, 422)
        return _render(request, role, build_soak_view(errors=errors, role=role), 422)
    try:
        payload = _soak(request).read()
    except HistoryUnavailable as exc:
        reason = _reason(exc)
        if not _wants_html(request):
            return JSONResponse({"detail": {"status": "unavailable", "reason": reason}}, 503)
        return _render(request, role, build_soak_view(unavailable=reason, role=role), 503)
    if not _wants_html(request):
        return JSONResponse(payload)
    return _render(request, role, build_soak_view(payload=payload, role=role), 200)


@router.post(EVIDENCE_PATH)
async def record_evidence(request: Request) -> Response:
    """Record one administrator attestation for a soak or readiness criterion."""

    role = _authorize(request, required_role="admin")
    body = parse_qs(
        (await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True
    )
    form = {key: values[0] for key, values in body.items()}
    submitted = parse_evidence(form)
    if submitted.errors:
        return await _refused(request, role, form, submitted.errors, 422)
    try:
        _soak(request).record_evidence(
            submitted.criterion,
            submitted.kind,
            submitted.status,
            submitted.note,
            recorded_by=role,
        )
    except HistoryUnavailable as exc:
        return await _refused(request, role, form, (_reason(exc),), 503)
    if not _wants_html(request):
        return JSONResponse({"status": "recorded"})
    return RedirectResponse(SOAK_PATH, status_code=303)


async def _refused(
    request: Request, role: str, form: dict[str, str], errors: Sequence[str], status_code: int
) -> Response:
    if not _wants_html(request):
        return JSONResponse({"detail": {"status": "refused", "errors": list(errors)}}, status_code)
    payload: dict[str, Any] | None
    unavailable: str | None
    try:
        payload = _soak(request).read()
        unavailable = None
    except HistoryUnavailable as exc:
        payload, unavailable = None, _reason(exc)
    view = build_soak_view(
        payload=payload,
        unavailable=unavailable,
        role=role,
        evidence_form=form,
        evidence_errors=errors,
    )
    return _render(request, role, view, status_code)


def _soak(request: Request) -> SqlAlchemySoak:
    soak = getattr(request.app.state, "soak", None)
    if soak is None:
        raise HistoryUnavailable(_NOT_CONFIGURED)
    return soak


def _reason(exc: HistoryUnavailable) -> str:
    return _NOT_CONFIGURED if str(exc) == _NOT_CONFIGURED else _UNREADABLE


def _render(request: Request, role: str, view: dict[str, Any], status_code: int) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="operator_soak.html",
        context={"page": _page(request, role), "view": view},
        status_code=status_code,
    )
