"""Presentation model for the System page (``GET /operator/system``).

``build_system`` regroups the operator snapshot, the saved unresolved orders and the bounded
diagnostics report into one page: the recorded cause and next safe step first, then an ordered
recovery checklist, the orders waiting on the venue, diagnostics, health facts, and the alert and
safety history. It adds no data and calls nothing. Every status is read from that evidence, so a
check that has no evidence says ``unavailable`` and an old one says ``stale``; none says
``verified`` because the page expects it to be fine. The checklist is guidance. It runs no
recovery and does not repeat the broker comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from api.dashboard import (
    _ORDER,
    _RUNTIME,
    ROW_LIMIT,
    Fact,
    Stamp,
    Status,
    _age,
    _history_order_href,
    _plain_amount,
    _stamp,
    _status,
    _transition,
    _unresolved,
    build_dashboard,
)

VERIFIED = Status("verified", "ok")
REQUIRED_WARN = Status("required", "warn")
REQUIRED_CRIT = Status("required", "crit")
UNAVAILABLE = Status("unavailable", "unknown")
STALE = Status("stale", "warn")

# A scheduled run is stale after this many missed intervals, the margin the market feeds use.
STALE_INTERVALS = 3
VALUE_LIMIT = 120
TRANSITION_NOTE = "The newest ten kill-switch changes are kept in this view."

_REARM_COPY = (
    "RUNNING does not repair causes. Re-arming only lowers the kill switch after an administrator "
    "confirms every checklist item and records the cause and approval. It resolves no order, "
    "fixes no divergence and restarts nothing."
)


@dataclass(frozen=True, slots=True)
class Step:
    """One ordered recovery check: what to look at, what the evidence says, where to go."""

    number: int
    title: str
    status: Status
    detail: str
    stamp: Stamp | None = None
    links: tuple[tuple[str, str], ...] = ()


def build_system(
    snapshot: Mapping[str, Any],
    *,
    role: str,
    diagnostics: Mapping[str, Any],
    reconcile_interval: float | None = None,
    persistence_configured: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the System page shows, from the payload the other operator views use.

    ``diagnostics`` is ``{"outcome": "report", "report": ...}``, ``{"outcome": "refused",
    "reason": ...}`` or ``{"outcome": "failed"}``; the route decides which, this only displays it.
    """

    now = now or datetime.now(UTC)
    dashboard = build_dashboard(snapshot, role=role, now=now)
    safety = dashboard["safety"]
    is_admin = role == "admin"
    orders = _orders(snapshot, now, is_admin=is_admin)
    cause = _cause(dashboard)
    return {
        "header": dashboard["header"],
        "safety": safety,
        "cause": cause,
        "steps": _steps(
            snapshot, dashboard, cause, orders, reconcile_interval, now, is_admin=is_admin
        ),
        "orders": orders,
        "diagnostics": _diagnostics(diagnostics, now),
        "health": _health_groups(dashboard, snapshot, persistence_configured),
        "strategies": dashboard["strategies"],
        "alerts": _alerts(dashboard),
        "errors": dashboard["errors"],
        "transitions": _transitions(snapshot, now),
        "kill_switch_states": dashboard["kill_switch_states"],
        "rearm": {
            "explanation": _REARM_COPY,
            "is_admin": is_admin,
            "running": safety.running,
            "checklist": dashboard["rearm_checklist"],
        },
        "legend": dashboard["legend"],
        "limit": ROW_LIMIT,
    }


def _cause(dashboard: Mapping[str, Any]) -> dict[str, Any]:
    """The recorded reason for a pause or halt, or the plain statement that none is recorded."""

    safety = dashboard["safety"]
    attention = dashboard["attention"]
    complete = bool(attention["available"])
    known = bool(safety.causes or safety.last_change)
    # The attention rows point at Overview anchors; from here they need the full address.
    others = [
        {**row, "href": f"/operator{row['href']}" if row["href"].startswith("#") else row["href"]}
        for row in attention.get("rows", ())
        if not row["title"].startswith("Kill switch ")
    ]
    return {
        "complete": complete,
        "incomplete_detail": "" if complete else attention["detail"],
        "known": known,
        "unavailable": (
            "Cause unavailable: no saved kill-switch change or recorded condition explains this "
            "state. Check Alerts & errors and the application log. Do not assume it is safe."
        ),
        "others": others,
        "no_incident": complete and not others and safety.running,
    }


def _orders(snapshot: Mapping[str, Any], now: datetime, *, is_admin: bool) -> dict[str, Any]:
    """Saved pending or unknown orders, linked to their persisted detail.

    ``available`` is false when the route could not read the saved orders, which is different
    from a list with nothing in it: only an empty readable list says none is waiting.
    """

    saved = _unresolved(snapshot, now)
    if saved is None:
        return {
            "state": Status("not checked", "unknown"),
            "readable": False,
            "rows": [],
            "detail": (
                "The saved orders were not read in this view, so none can be confirmed resolved. "
                "Open order history and look up any pending or unknown order by client order ID."
            ),
            "is_admin": is_admin,
        }
    if not saved["readable"]:
        return {
            "state": Status("unreadable", "crit"),
            "readable": False,
            "rows": [],
            "detail": (
                "The saved orders could not be read, so none can be closed from here and none can "
                "be confirmed resolved. Check Alerts & errors and the application log."
            ),
            "is_admin": is_admin,
        }
    rows = [{**row, "href": _history_order_href(row["client_order_id"])} for row in saved["rows"]][
        :ROW_LIMIT
    ]
    total = len(saved["rows"])
    return {
        "state": Status(f"{total} waiting", "crit" if total else "ok")
        if total
        else Status("none waiting", "ok"),
        "readable": True,
        "rows": rows,
        "total": total,
        "detail": "",
        "is_admin": is_admin,
    }


_RANK = {"crit": 4, "warn": 3, "unknown": 2, "neutral": 1, "ok": 0}


def _steps(
    snapshot: Mapping[str, Any],
    dashboard: Mapping[str, Any],
    cause: Mapping[str, Any],
    orders: Mapping[str, Any],
    interval: float | None,
    now: datetime,
    *,
    is_admin: bool,
) -> list[Step]:
    safety = dashboard["safety"]
    recovery = snapshot.get("recovery", {})
    reconciliation = snapshot.get("reconciliation", {})
    connectivity = snapshot.get("connectivity", {})
    portfolio = dashboard["portfolio"]
    steps: list[Step] = []

    def add(
        title: str,
        status: Status,
        detail: str,
        stamp: Stamp | None = None,
        *links: tuple[str, str],
    ) -> None:
        steps.append(
            Step(
                len(steps) + 1, title, status, detail, stamp if stamp and stamp.iso else None, links
            )
        )

    # 1. The recorded cause.
    if safety.running:
        add(
            "Read the recorded cause",
            VERIFIED,
            "The kill switch is running, so there is no pause or halt to explain. Running "
            "does not prove the system is healthy or trading.",
        )
    elif cause["known"]:
        add(
            "Read the recorded cause",
            REQUIRED_CRIT if safety.halted else REQUIRED_WARN,
            "A cause is recorded above. Read it before touching anything else.",
            None,
            ("Kill-switch history", "/operator/history/risk"),
        )
    else:
        add(
            "Read the recorded cause",
            UNAVAILABLE,
            "No saved change or condition explains this state. Do not infer one from the state.",
            None,
            ("Kill-switch history", "/operator/history/risk"),
            ("Events", "/operator/history/events"),
        )

    # 2. Orders waiting on the venue.
    if not orders["readable"]:
        add(
            "Resolve orders waiting on the venue",
            UNAVAILABLE if orders["state"].tone == "unknown" else REQUIRED_CRIT,
            orders["detail"],
            None,
            ("Order history", "/operator/history/orders"),
        )
    elif orders["total"]:
        add(
            "Resolve orders waiting on the venue",
            REQUIRED_CRIT,
            f"{orders['total']} saved order(s) are pending or unknown. Look each one up by its "
            "client order ID. Never resubmit one.",
            None,
            ("Orders below", "#orders"),
            ("Diagnostics", "/operator/diagnostics"),
        )
    else:
        add(
            "Resolve orders waiting on the venue",
            VERIFIED,
            "No saved order is pending or unknown at this page load.",
            None,
            ("Order history", "/operator/history/orders"),
        )

    # 3. Startup recovery.
    recovered = _stamp(recovery.get("completed_at"), now)
    recovery_word = str(recovery.get("status", "not_run"))
    recovery_detail = str(recovery.get("detail") or "no detail recorded")
    if recovery_word == "reconciled":
        add(
            "Check startup recovery",
            VERIFIED,
            f"Startup recovery reported reconciled: {recovery_detail}. This covers process "
            "start only; it is not a current comparison with the broker.",
            recovered,
        )
    elif recovery_word == "halted":
        add(
            "Check startup recovery",
            REQUIRED_CRIT,
            f"Startup recovery halted: {recovery_detail}.",
            recovered,
            ("Discrepancies", "/operator/history/discrepancies"),
        )
    else:
        add(
            "Check startup recovery",
            UNAVAILABLE,
            f"Startup recovery is {recovery_word.replace('_', ' ')}: {recovery_detail}. "
            "Nothing was compared with a broker at start.",
            recovered,
        )

    # 4. The newest scheduled reconciliation.
    result = str(reconciliation.get("last_result", "not_scheduled"))
    last_run = _stamp(reconciliation.get("last_run_at"), now)
    differences = reconciliation.get("last_discrepancies")
    if result == "clean":
        age = _seconds_since(reconciliation.get("last_run_at"), now)
        if age is None:
            add(
                "Review the latest scheduled reconciliation",
                UNAVAILABLE,
                "The last run is reported clean, but its time is not readable, so its age "
                "cannot be judged.",
                None,
                ("Trends", "/operator/history/trends"),
            )
        elif interval is not None and age > interval * STALE_INTERVALS:
            add(
                "Review the latest scheduled reconciliation",
                STALE,
                f"The last run was clean but is older than {STALE_INTERVALS} intervals. It says "
                "nothing about the broker now.",
                last_run,
                ("Trends", "/operator/history/trends"),
            )
        else:
            add(
                "Review the latest scheduled reconciliation",
                VERIFIED,
                "The newest scheduled run found no difference. This page does not run one, and "
                "a clean result is not proof that the next run will be clean.",
                last_run,
                ("Trends", "/operator/history/trends"),
            )
    elif result == "diverged":
        count = f"{differences} difference(s)" if isinstance(differences, int) else "differences"
        add(
            "Review the latest scheduled reconciliation",
            REQUIRED_CRIT,
            f"The newest scheduled run found {count} with the broker. The broker is authoritative.",
            last_run,
            ("Discrepancies", "/operator/history/discrepancies"),
            ("Diagnostics", "/operator/diagnostics"),
        )
    elif result == "unavailable":
        add(
            "Review the latest scheduled reconciliation",
            REQUIRED_CRIT,
            "The newest scheduled run could not read the broker, so nothing was compared.",
            last_run,
            ("Discrepancies", "/operator/history/discrepancies"),
        )
    else:
        add(
            "Review the latest scheduled reconciliation",
            UNAVAILABLE,
            "No scheduled reconciliation has run in this process"
            + (" (none is scheduled)." if result == "not_scheduled" else " yet."),
        )

    # 5. Runtime and strategy heartbeat.
    runtime = snapshot.get("runtime", {})
    heartbeats = dashboard["strategies"]
    runtime_status = _status(runtime.get("status"), _RUNTIME)
    worst = max(heartbeats, key=lambda row: _RANK[row["status"].tone], default=None)
    tones = [runtime_status.tone] + ([worst["status"].tone] if worst else [])
    parts = [f"Paper runtime {runtime_status.word}: {runtime.get('detail') or 'no detail'}."]
    if worst is None:
        parts.append("No strategy heartbeat is registered.")
        tones.append("unknown")
    else:
        parts.append(
            f"Strategy {worst['name']} ({worst['version']}) is {worst['status'].word}: "
            f"{worst['detail'] or 'no detail'}."
        )
    tone = max(tones, key=_RANK.__getitem__)
    add(
        "Check the runtime and strategy heartbeat",
        VERIFIED
        if tone == "ok"
        else REQUIRED_CRIT
        if tone == "crit"
        else REQUIRED_WARN
        if tone == "warn"
        else UNAVAILABLE,
        " ".join(parts),
        worst["last_seen"] if worst else None,
    )

    # 6. Broker connectivity and the portfolio snapshot.
    broker_word = str(connectivity.get("status", "not_configured"))
    checked = _stamp(connectivity.get("checked_at"), now)
    if broker_word == "healthy" and portfolio["current"]:
        add(
            "Check broker access and the portfolio snapshot",
            VERIFIED,
            "The broker was read when this page loaded. Current means the read succeeded; it "
            "does not mean the values match the venue's own record.",
            checked,
        )
    elif broker_word == "unavailable" or portfolio["last_known"]:
        add(
            "Check broker access and the portfolio snapshot",
            REQUIRED_CRIT,
            "The broker could not be read at page load. Any balances shown are last known and "
            "may not match the broker.",
            checked,
        )
    else:
        add(
            "Check broker access and the portfolio snapshot",
            UNAVAILABLE,
            str(connectivity.get("detail") or "No broker state is available."),
            checked,
        )

    # 7. The administrator re-arm.
    if safety.running:
        add(
            "Re-arm the kill switch",
            VERIFIED,
            "Nothing to re-arm: the kill switch is running. " + _REARM_COPY,
        )
    else:
        add(
            "Re-arm the kill switch",
            REQUIRED_CRIT if safety.halted else REQUIRED_WARN,
            _REARM_COPY
            + ("" if is_admin else " Your operator role cannot re-arm; ask an administrator."),
            None,
            *((("Re-arm review", "/operator/rearm"),) if is_admin else ()),
        )
    return steps


def _seconds_since(value: object, now: datetime) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds()


def _health_groups(
    dashboard: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    persistence_configured: bool,
) -> list[dict[str, Any]]:
    """The dashboard's health facts plus persistence, each unchanged."""

    transitions = snapshot.get("risk", {}).get("transitions") or ()
    persistence = {
        "title": "Persistence and history",
        "facts": [
            Fact(
                "Stored history and diagnostics",
                "status",
                status=Status("configured", "ok")
                if persistence_configured
                else Status("not configured", "unknown"),
            ),
            Fact(
                "Recorded kill-switch changes",
                "count",
                str(len(transitions)),
                note=TRANSITION_NOTE,
            ),
        ],
    }
    return [*dashboard["health"], persistence]


def _alerts(dashboard: Mapping[str, Any]) -> dict[str, Any]:
    alerts = dashboard["alerts"]
    failed = [
        alert
        for alert in [*alerts["rows"], *alerts["dismissed_rows"]]
        if any(item["status"].word == "failed" for item in alert["deliveries"])
    ]
    return {**alerts, "failed": failed, "destinations": dashboard["destinations"]}


def _transitions(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    items = snapshot.get("risk", {}).get("transitions") or ()
    rows = [_transition(item, now) for item in items][:ROW_LIMIT]
    return {
        "rows": rows,
        "recorded": bool(rows),
        "empty": (
            "No kill-switch change is recorded in this view. That is not proof that nothing "
            "changed: the state may have been set before auditing began or by the state file."
        ),
    }


# Diagnostics -------------------------------------------------------------------------------


def _clip(value: object) -> str:
    text = str(value)
    return text if len(text) <= VALUE_LIMIT else text[: VALUE_LIMIT - 1] + "…"


def _lookup_status(answer: str) -> Status:
    """How the venue answered a pending order's lookup, as a pill; the full answer is kept."""

    if answer.startswith("found"):
        return Status("venue has it", "warn")
    if answer == "not found":
        return Status("venue has no record", "warn")
    if answer.startswith("lookup timed out"):
        return Status("lookup timed out", "unknown")
    if answer.startswith("lookup failed"):
        return Status("lookup failed", "unknown")
    return Status("not asked", "unknown")


def _section_reason(section: Mapping[str, Any]) -> str:
    return str(section.get("reason") or "not readable")


def _diagnostics(diagnostics: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    outcome = diagnostics.get("outcome")
    if outcome == "refused":
        return {
            "state": Status("refused", "warn"),
            "outcome": "refused",
            "detail": str(diagnostics.get("reason", "diagnostics were refused")),
        }
    if outcome != "report":
        return {
            "state": Status("unavailable", "unknown"),
            "outcome": "failed",
            "detail": (
                "The diagnostics report could not be built. This is not a clean result. "
                "The reason was written to the application log, not to this page."
            ),
        }
    report = diagnostics["report"]
    sections = {
        "pending": _pending(report.get("pending_orders", {}), now),
        "discrepancies": _discrepancy_rows(report.get("discrepancies", {}), now),
        "fills": _fill_rows(report.get("fills", {}), now),
    }
    unreadable = [name for name, part in sections.items() if not part["readable"]]
    return {
        "state": Status("partly unavailable", "warn") if unreadable else Status("read", "neutral"),
        "outcome": "report",
        "detail": (
            f"{len(unreadable)} section(s) could not be read; the others are shown."
            if unreadable
            else "Read-only. Nothing here changes an order or the kill switch."
        ),
        "generated": _stamp(report.get("generated_at"), now),
        "limit": report.get("limit"),
        **sections,
    }


def _pending(section: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    if not section.get("readable"):
        return {"readable": False, "reason": _section_reason(section), "rows": []}
    rows = [
        {
            "ref": str(order.get("ref", "")),
            "status": _status(order.get("status"), _ORDER),
            "symbol": str(order.get("symbol") or "unknown"),
            "side": str(order.get("side") or "unknown"),
            "quantity": _plain_amount(order["quantity"]) if order.get("quantity") else "unknown",
            "age": _age(timedelta(seconds=int(order.get("age_seconds", 0)))).replace(" ago", ""),
            "answer": str(order.get("broker", "")),
            "venue": _lookup_status(str(order.get("broker", ""))),
        }
        for order in section.get("orders", ())
    ]
    return {"readable": True, "rows": rows[:ROW_LIMIT]}


def _discrepancy_rows(section: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    if not section.get("readable"):
        return {"readable": False, "reason": _section_reason(section), "rows": []}
    rows = [
        {
            "at": _stamp(row.get("at"), now),
            "kind": str(row.get("kind", "")),
            "key": str(row.get("key", "")),
            "safety_action": str(row.get("safety_action", "")),
            "fields": [
                {
                    "field": str(change.get("field", "")),
                    "local": _clip(change.get("local")),
                    "broker": _clip(change.get("broker")),
                    "delta": str(change["delta"]) if change.get("delta") else "",
                }
                for change in row.get("fields", ())
            ],
        }
        for row in section.get("rows", ())
    ]
    return {"readable": True, "rows": rows}


def _fill_rows(section: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    if not section.get("readable"):
        return {"readable": False, "reason": _section_reason(section), "rows": [], "checks": []}
    rows = [
        {
            "at": _stamp(row.get("at"), now),
            "ref": str(row.get("ref", "")),
            "order": str(row.get("order", "")),
            "symbol": str(row.get("symbol", "")),
            "side": str(row.get("side", "")),
            "quantity": str(row.get("quantity", "")),
            "price": str(row.get("price", "")),
            "fee": str(row.get("fee", "")),
        }
        for row in section.get("rows", ())
    ]
    checks = []
    for check in section.get("checks", ()):
        word = str(check.get("status", ""))
        checks.append(
            {
                "fills": ", ".join(str(item) for item in check.get("fills", ())),
                "from": _stamp(check.get("from"), now) if check.get("from") else None,
                "to": _stamp(check.get("to"), now) if check.get("to") else None,
                "status": word,
                "itemized": word == "itemized",
                "assets": [
                    {
                        "asset": str(asset.get("asset", "")),
                        "difference": str(asset.get("difference", "")),
                    }
                    for asset in check.get("assets", ())
                ],
            }
        )
    return {"readable": True, "rows": rows, "checks": checks}
