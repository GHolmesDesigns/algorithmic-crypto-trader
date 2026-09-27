"""Authenticated, bounded research and replay routes."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from api.research import ResearchRunRequest, ResearchWorkspace
from api.research_view import build_research_view
from api.routes import _authorize, _page, _wants_html, templates

router = APIRouter()
PATH = "/operator/research"


@router.get(PATH)
async def research_workspace(request: Request) -> Response:
    role = _authorize(request)
    workspace = _workspace(request)
    selected = request.query_params.getlist("run")
    view = _view(workspace, selected=selected)
    return _render_or_json(request, role, view)


@router.get(PATH + "/compare")
async def compare_runs(request: Request) -> Response:
    role = _authorize(request)
    workspace = _workspace(request)
    selected = request.query_params.getlist("run")
    view = _view(workspace, selected=selected)
    return _render_or_json(request, role, view)


@router.post(PATH + "/runs")
async def submit_research_run(request: Request) -> Response:
    role = _authorize(request)
    workspace = _workspace(request)
    try:
        values = await _request_values(request)
        submitted = ResearchRunRequest.from_mapping(values)
        job = await workspace.submit(submitted)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error(request, role, [str(exc)], status_code=422)
    if _wants_html(request):
        return RedirectResponse(f"{PATH}/runs/{job.run_id}", status_code=303)
    return JSONResponse(job.summary(), status_code=202)


@router.get(PATH + "/runs/{run_id}/export")
async def export_research_run(request: Request, run_id: str) -> Response:
    _authorize(request)
    workspace = _workspace(request)
    try:
        payload = workspace.export(run_id)
    except KeyError:
        return JSONResponse({"detail": "research run not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return Response(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="research-{run_id}.json"'},
    )


@router.get(PATH + "/runs/{run_id}")
async def research_run(request: Request, run_id: str) -> Response:
    role = _authorize(request)
    workspace = _workspace(request)
    job = workspace.get(run_id)
    if job is None:
        return JSONResponse({"detail": "research run not found"}, status_code=404)
    view = _view(workspace, job=job.summary())
    return _render_or_json(request, role, view, status_code=200)


def _workspace(request: Request) -> ResearchWorkspace:
    workspace = getattr(request.app.state, "research", None)
    if not isinstance(workspace, ResearchWorkspace):
        raise RuntimeError("research workspace is not initialized")
    return workspace


def _view(
    workspace: ResearchWorkspace,
    *,
    selected: list[str] | None = None,
    job: dict[str, object] | None = None,
    errors: list[str] | None = None,
) -> dict[str, object]:
    selected = selected or []
    return build_research_view(
        catalog=workspace.catalog(),
        strategies=workspace.strategies(),
        jobs=[item.summary() for item in reversed(tuple(workspace.jobs.values()))],
        compare=workspace.compare(selected),
        selected=selected,
        job=job,
        errors=errors or [],
    )


def _render_or_json(
    request: Request, role: str, view: dict[str, object], *, status_code: int = 200
) -> Response:
    if not _wants_html(request):
        return JSONResponse(view, status_code=status_code)
    return templates.TemplateResponse(
        request=request,
        name="operator_research.html",
        context={"page": _page(request, role), "view": view},
        status_code=status_code,
    )


def _error(request: Request, role: str, errors: list[str], *, status_code: int) -> Response:
    workspace = _workspace(request)
    if not _wants_html(request):
        return JSONResponse({"detail": {"status": "refused", "errors": errors}}, status_code=422)
    return _render_or_json(request, role, _view(workspace, errors=errors), status_code=status_code)


async def _request_values(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        value = await request.json()
        if not isinstance(value, dict):
            raise TypeError("research request must be a JSON object")
        return value
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {key: item[-1] for key, item in values.items()}
