"""Presentation model for the Soak & readiness page.

``build_soak_view`` turns the soak payload, the same one the JSON route returns,
into display rows. Like the other views, it adds no data and calls nothing. A day
or a criterion ends in one of three states, and none looks like another:

- ``pass``: every field for that day, or the latest evidence for that criterion,
  says so;
- ``fail``: the latest evidence for a criterion says so;
- ``incomplete``: a day is missing a field with no producer yet, or a criterion
  has no evidence record at all.

Nothing here is ever "verified live": that phrase does not appear on this page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from api.dashboard import Stamp, _stamp, _status
from api.history_view import NAV, PATHS
from api.soak import CRITERIA, EVIDENCE_KINDS, EVIDENCE_STATUSES, NOTE_LIMIT

SOAK_PATH = PATHS["soak"]
EVIDENCE_PATH = f"{SOAK_PATH}/evidence"
INTRO = (
    "A 30-day digest from the persisted records, and the #12 soak and #13 readiness "
    "criteria. Everything defaults to incomplete until evidence exists: a day never shows "
    "pass on partial data, and a criterion never shows pass without a recorded attestation."
)
_DAY_STATUS = {"incomplete": "unknown", "pass": "ok"}
_CRITERION_STATUS = {"incomplete": "unknown", "pass": "ok", "fail": "crit"}
_MISSING_LABELS = {
    "equity": "Equity",
    "uptime_restarts_disconnects": "Uptime, restarts, and disconnects",
    "backup_restore": "Backup and restore",
    "incidents": "Incidents",
}


def build_soak_view(
    *,
    payload: Mapping[str, Any] | None = None,
    unavailable: str | None = None,
    errors: Sequence[str] = (),
    role: str = "operator",
    evidence_form: Mapping[str, str] | None = None,
    evidence_errors: Sequence[str] = (),
    evidence_saved: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the Soak & readiness page shows; ``payload`` is absent when nothing was read."""

    now = now or datetime.now(UTC)
    form = evidence_form or {}
    view: dict[str, Any] = {
        "kind": "soak",
        "title": "Soak & readiness",
        "intro": INTRO,
        "path": SOAK_PATH,
        "evidence_path": EVIDENCE_PATH,
        "nav": [
            {"href": PATHS[item], "label": label, "current": item == "soak"}
            for item, _, label in NAV
        ],
        "errors": list(errors),
        "unavailable": unavailable,
        "window": None,
        "days": [],
        "digest_gaps": [],
        "criteria": {"soak": [], "readiness": []},
        "can_record_evidence": role == "admin",
        "evidence_kinds": list(EVIDENCE_KINDS),
        "evidence_statuses": list(EVIDENCE_STATUSES),
        "evidence_form": {
            "criterion": form.get("criterion", ""),
            "kind": form.get("kind", ""),
            "status": form.get("status", ""),
            "note": form.get("note", ""),
        },
        "evidence_errors": list(evidence_errors),
        "evidence_saved": evidence_saved,
        "note_limit": NOTE_LIMIT,
    }
    if payload is None:
        return view
    window = payload["window"]
    view["window"] = {
        "since": _moment(window["since"]),
        "until": _moment(window["until"]),
        "as_of": _moment(window["as_of"]),
    }
    view["days"] = [_day(day, now) for day in payload["days"]]
    view["digest_gaps"] = [
        {"label": _MISSING_LABELS.get(gap["key"], gap["key"]), "reason": gap["reason"]}
        for gap in payload["digest_gaps"]
    ]
    view["criteria"] = {
        "soak": [_criterion(item, now) for item in payload["criteria"]["soak"]],
        "readiness": [_criterion(item, now) for item in payload["criteria"]["readiness"]],
    }
    return view


def _day(day: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "date": str(day["date"]),
        "in_progress": bool(day["in_progress"]),
        "trades": int(day["trades"]),
        "refusals": int(day["refusals"]),
        "reconciliation_runs": int(day["reconciliation_runs"]),
        "divergences": int(day["divergences"]),
        "kill_switch_events": int(day["kill_switch_events"]),
        "equity": day.get("equity"),
        "status": _status(day["status"], _DAY_STATUS),
        "missing": [_MISSING_LABELS.get(key, key) for key in day.get("missing", ())],
    }


def _criterion(item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    evidence = item.get("evidence")
    return {
        "key": item["key"],
        "label": CRITERIA.get(item["key"], item["key"]),
        "status": _status(item["status"], _CRITERION_STATUS),
        "evidence": _evidence(evidence, now) if evidence else None,
    }


def _evidence(evidence: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "kind_label": str(evidence.get("kind_label", "")),
        "status": str(evidence.get("status", "")),
        "note": str(evidence.get("note", "")),
        "recorded_by": str(evidence.get("recorded_by", "")),
        "recorded_at": _stamp(evidence.get("recorded_at"), now),
    }


def _moment(value: str) -> Stamp:
    moment = datetime.fromisoformat(value).astimezone(UTC)
    return Stamp(moment.isoformat(), moment.strftime("%Y-%m-%d %H:%M UTC"))
