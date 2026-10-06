"""Presentation model for the operator history pages.

``build_history_view`` turns a history payload, the same one the JSON routes
return, into display rows. Like the dashboard, it adds no data and calls nothing.
Each value carries a tone and a word, so "0 recorded", "not recorded", and "not
available" never look alike: a count is a count, a missing link in a lineage says
it was not recorded, and a history that could not be read says so instead of
showing an empty table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from execution.audit import ORDER_CLOSED_EVENT
from risk.engine import RISK_GATES

from api.dashboard import _KILL_SWITCH, _ORDER, Stamp, Status, _stamp, _status
from api.history import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW,
    FILTERS,
    GATE_LABELS,
    MAX_LIMIT,
    MAX_WINDOW,
    TEXT_LIMIT,
    WINDOWS,
    HistoryQuery,
)

HISTORY_PATH = "/operator/history"
NAV = (
    ("orders", "orders", "Orders"),
    ("signals", "signals", "Signals"),
    ("risk_decisions", "risk-decisions", "Risk decisions"),
    ("risk", "risk", "Risk & safety"),
    ("discrepancies", "discrepancies", "Discrepancies"),
    ("events", "events", "System events"),
    ("trends", "trends", "Trends"),
    ("soak", "soak", "Soak & readiness"),
)
PATHS = {kind: f"{HISTORY_PATH}/{slug}" for kind, slug, _ in NAV}
PAGES: dict[str, tuple[str, str, str]] = {
    # kind: (title, noun, introduction)
    "orders": (
        "Orders",
        "orders",
        "Every persisted order with its fills, newest first. Expand an order for its "
        "lineage: signal, risk decision, order, and fills.",
    ),
    "signals": (
        "Signals",
        "signals",
        "Every recorded strategy signal and what became of it: its risk decision and, "
        "when approved, its order.",
    ),
    "risk_decisions": (
        "Risk decisions",
        "risk decisions",
        "Every risk evaluation, approved or refused. A refusal names the first gate that "
        "failed and why.",
    ),
    "risk": (
        "Risk & safety",
        "risk decisions",
        "Refusals by ordered risk gate, the latest refusal, and kill-switch history.",
    ),
    "discrepancies": (
        "Discrepancies",
        "discrepancies",
        "Differences reconciliation found between local and broker state; the broker's "
        "record was adopted. Values stay in the database: only the names of the fields "
        "that differ are shown.",
    ),
    "events": (
        "System events",
        "system events",
        "Recorded system events, such as kill-switch changes. Only the fields each event "
        "type is known to carry are shown.",
    ),
    "lineage": (
        "Order lineage",
        "orders",
        "One order from its signal through its risk decision to its fills, rebuilt from "
        "the persisted records.",
    ),
}

# Every order status, with its tone and what an operator does about it.
ORDER_STATUS_MEANINGS = (
    ("pending_submit", "Saved before submission; the broker has not confirmed it."),
    (
        "unknown",
        "The submission outcome is ambiguous. Look it up by client order ID. Never resubmit it.",
    ),
    ("open", "Accepted by the broker and working."),
    ("partially_filled", "Part of the quantity has filled; the rest is working."),
    ("filled", "Completely filled, with its fills recorded."),
    ("canceled", "Canceled. Any partial fills stay recorded."),
    ("rejected", "The broker refused it."),
)
_OUTCOME = {"approved": "ok", "refused": "warn"}
_SAFETY_ACTION = {"halted": "crit", "none": "neutral"}
_ACTORS = {"operator": "Operator", "admin": "Administrator", "system": "The system"}
_DISCONNECT_KINDS = {
    "connect_failed": "Could not connect",
    "subscribe_failed": "Subscription failed",
    "heartbeat_timeout": "Heartbeat timed out",
    "stale_data": "Data went stale",
    "parse_error": "Message could not be parsed",
    "closed_by_peer": "Closed by the exchange",
    "unknown": "Unknown cause",
    "not_recorded": "Reason not recorded",
}
_GAPS = {
    "signal": "Signal not recorded.",
    "strategy_version": "The signal's strategy version does not match the order's.",
    "risk_decision": "Risk decision not recorded.",
    "risk_decision_not_approved": "The risk decision this order cites is a refusal.",
    "fills": "The order executed, but no fills are recorded.",
}
_FIELD_LABELS = {
    "symbol": "Symbol",
    "status": "Status",
    "strategy_version": "Strategy version",
    "client_order_id": "Client order ID",
    "correlation_id": "Correlation ID",
    "outcome": "Outcome",
    "failed_gate": "Failed gate",
    "entity_type": "Entity",
    "event_type": "Event type",
}
_WINDOW_LABELS = {
    "1h": "Last hour",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "31d": "Last 31 days",
}


def build_history_view(
    kind: str,
    *,
    form: Mapping[str, str],
    query: HistoryQuery | None = None,
    payload: Mapping[str, Any] | None = None,
    errors: Sequence[str] = (),
    unavailable: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything a history page shows; ``payload`` is absent when nothing was read."""

    now = now or datetime.now(UTC)
    title, noun, intro = PAGES[kind]
    view: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "noun": noun,
        "intro": intro,
        "path": PATHS.get(kind, PATHS["orders"]),
        "nav": [
            {"href": PATHS[item], "label": label, "current": item == kind} for item, _, label in NAV
        ],
        "fields": _fields(kind, form),
        "errors": list(errors),
        "unavailable": unavailable,
        "window": None,
        "summary": None,
        "rows": [],
        "next_href": None,
        "newest_href": None,
        "statuses": [
            {"status": Status(word, _ORDER[word]), "meaning": meaning}
            for word, meaning in ORDER_STATUS_MEANINGS
        ],
        "limits": {"days": MAX_WINDOW.days, "rows": MAX_LIMIT},
    }
    if payload is None:
        return view
    if query is not None:
        view["window"] = {"since": _stamp(query.since, now), "until": _stamp(query.until, now)}
    if kind == "risk":
        view["risk"] = _risk(payload, now, query)
        view["rows"] = [_event_row(row, now) for row in payload["kill_switch"]["rows"]]
        return view
    rows = payload.get("rows", ())
    view["rows"] = [_ROWS[kind](row, now) for row in rows]
    total = int(payload.get("total", 0))
    view["summary"] = {
        "total": total,
        "shown": len(rows),
        "continued": bool(query and query.before),
    }
    if query is not None and payload.get("next_before"):
        view["next_href"] = _href(view["path"], query.params(before=payload["next_before"]))
    if query is not None and query.before is not None:
        # A preset window follows "now"; an explicit start keeps its end so it stays bounded.
        newest = query.params(before=None, until=None if query.window else query.until.isoformat())
        view["newest_href"] = _href(view["path"], newest)
    return view


def build_lineage_view(
    row: Mapping[str, Any] | None,
    *,
    client_order_id: str,
    unavailable: str | None = None,
    problem: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One order's lineage; ``problem`` says why none is shown when it was not found."""

    now = now or datetime.now(UTC)
    view = build_history_view("lineage", form={}, unavailable=unavailable, now=now)
    view["client_order_id"] = client_order_id
    view["problem"] = problem
    view["order"] = _order_row(row, now) if row is not None else None
    return view


def _href(path: str, params: Mapping[str, str]) -> str:
    return f"{path}?{urlencode(params)}" if params else path


def _fields(kind: str, form: Mapping[str, str]) -> list[dict[str, Any]]:
    if kind not in FILTERS or kind == "lineage":
        return []
    fields: list[dict[str, Any]] = []
    for item in FILTERS[kind]:
        field: dict[str, Any] = {
            "name": item.name,
            "label": _FIELD_LABELS[item.name],
            "value": form.get(item.name, ""),
            "type": "text",
            "options": None,
            "maxlength": 36 if item.kind == "uuid" else TEXT_LIMIT,
        }
        if item.kind == "choice":
            field["options"] = [("", "Any")] + [
                (choice, _choice_label(item.name, choice)) for choice in item.choices
            ]
        fields.append(field)
    window = form.get("window", "" if form.get("since") else DEFAULT_WINDOW)
    fields += [
        {
            "name": "window",
            "label": "Time window",
            "value": window,
            "type": "select",
            "options": [(key, _WINDOW_LABELS[key]) for key in WINDOWS]
            + [("", "From the start time")],
            "maxlength": None,
        },
        {
            "name": "since",
            "label": "Start (UTC)",
            "value": _local(form.get("since", "")),
            "type": "datetime-local",
            "options": None,
            "maxlength": None,
        },
        {
            "name": "until",
            "label": "End (UTC, blank for now)",
            "value": _local(form.get("until", "")),
            "type": "datetime-local",
            "options": None,
            "maxlength": None,
        },
    ]
    if kind != "risk":
        fields.append(
            {
                "name": "limit",
                "label": "Rows per page",
                "value": form.get("limit", str(DEFAULT_LIMIT)),
                "type": "select",
                "options": [(str(size), str(size)) for size in (25, 50, MAX_LIMIT)],
                "maxlength": None,
            }
        )
    return fields


def _choice_label(name: str, choice: str) -> str:
    if name == "failed_gate":
        position = _gate_position(choice)
        label = GATE_LABELS[choice]
        return f"{position}. {label}" if position else f"Before the gates: {label}"
    return choice.replace("_", " ")


def _gate_position(gate: str) -> int | None:
    names = [name for name, _ in RISK_GATES]
    return names.index(gate) + 1 if gate in names else None


def _local(value: str) -> str:
    """An ISO time as a browser date-time field value, in UTC; other text as given."""

    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _copy(value: object) -> dict[str, Any] | None:
    return {"text": str(value)} if value not in (None, "") else None


def _text(value: object, missing: str = "not recorded") -> dict[str, Any]:
    if value in (None, ""):
        return {"absent": missing}
    return {"text": str(value)}


def _lineage_href(client_order_id: object) -> str | None:
    try:
        return f"{PATHS['orders']}/{UUID(str(client_order_id))}"
    except ValueError:
        return None


def _gate(decision: Mapping[str, Any]) -> str:
    gate = decision.get("failed_gate")
    if not gate:
        return ""
    label = decision.get("gate_label") or "Unrecognised gate"
    position = decision.get("gate_position")
    if position:
        return f"Gate {position} of {len(RISK_GATES)}: {label}"
    return f"Before the gates: {label}" if gate in GATE_LABELS else f"{label} ({gate})"


def _signal_summary(signal: Mapping[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "signal_id": _copy(signal.get("signal_id")),
        "text": " ".join(
            str(part)
            for part in (signal.get("symbol"), signal.get("side"), signal.get("quantity"))
            if part
        ),
        "strategy_version": str(signal.get("strategy_version", "")),
        "created": _stamp(signal.get("created_at"), now),
    }


def _decision_summary(decision: Mapping[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "outcome": _status(decision.get("outcome"), _OUTCOME),
        "gate": _gate(decision),
        "reason": str(decision.get("reason") or ""),
        "decided": _stamp(decision.get("decided_at"), now),
        "approval_id": _copy(decision.get("approval_id")),
        "correlation_id": _copy(decision.get("correlation_id")),
    }


def _order_row(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    fills = list(row.get("fills", ()))
    count = int(row.get("fill_count", 0))
    return {
        "client_order_id": _copy(row.get("client_order_id")),
        "correlation_id": _copy(row.get("correlation_id")),
        "href": _lineage_href(row.get("client_order_id")),
        "symbol": _text(row.get("symbol")),
        "side": str(row.get("side") or ""),
        "order_type": str(row.get("order_type") or ""),
        "quantity": _text(row.get("quantity")),
        "limit_price": _text(
            row.get("limit_price"),
            "none: market order" if row.get("order_type") == "market" else "not recorded",
        ),
        "strategy_version": str(row.get("strategy_version", "")),
        "status": _status(row.get("status"), _ORDER),
        "rule": row.get("rule"),
        "closure": _closure_view(row.get("closure"), now),
        "created": _stamp(row.get("created_at"), now),
        "filled": str(row.get("filled_quantity") or "0"),
        "fill_count": count,
        "fills": [
            {
                "fill_id": str(item.get("fill_id", "")),
                "quantity": str(item.get("quantity", "")),
                "price": str(item.get("price", "")),
                "fee": str(item.get("fee", "")),
                "occurred": _stamp(item.get("occurred_at"), now),
            }
            for item in fills
        ],
        "more_fills": max(count - len(fills), 0),
        "signal": _signal_summary(row.get("signal"), now),
        "decision": _decision_summary(row.get("risk_decision"), now),
        "gaps": [_GAPS.get(gap, gap) for gap in row.get("gaps", ())],
    }


def _signal_row(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    decision = row.get("decision")
    order = row.get("order")
    if order is not None:
        why = "An order was placed."
    elif decision is None:
        why = "No risk decision is recorded, so no order was placed."
    elif decision.get("outcome") == "refused":
        why = f"No order: refused at {_gate(decision) or 'an unrecorded gate'}."
    else:
        why = "Approved, but no order is recorded."
    return {
        "signal": _signal_summary(row, now),
        "decision": _decision_summary(decision, now),
        "why": why,
        "order": _order_link(order),
    }


def _order_link(order: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if order is None:
        return None
    return {
        "client_order_id": str(order.get("client_order_id", "")),
        "href": _lineage_href(order.get("client_order_id")),
        "status": _status(order.get("status"), _ORDER),
    }


def _decision_row(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "decision": _decision_summary(row, now),
        "signal": _signal_summary(row.get("signal"), now),
        "order": _order_link(row.get("order")),
    }


def _discrepancy_row(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    local, broker = bool(row.get("local_recorded")), bool(row.get("broker_recorded"))
    if local and not broker:
        presence = "Recorded locally; missing at the broker."
    elif broker and not local:
        presence = "Reported by the broker; missing locally."
    elif not (local or broker):
        presence = "Neither side recorded a value."
    else:
        differs = list(row.get("differs", ()))
        presence = f"Differs in {', '.join(differs)}." if differs else "Both sides recorded."
    entity = str(row.get("entity_type", ""))
    return {
        "created": _stamp(row.get("created_at"), now),
        "entity_type": entity,
        "entity_key": str(row.get("entity_key", "")),
        "href": _lineage_href(row.get("entity_key")) if entity == "order" else None,
        "presence": presence,
        "action": _status(row.get("safety_action"), _SAFETY_ACTION),
    }


def _closure_view(closure: Mapping[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """An administrator's closure of a never-received order, as the order's page shows it."""

    if closure is None:
        return None
    return {
        "label": "Closed by an administrator: never received by the venue",
        "actor": _ACTORS.get(str(closure.get("actor")), "Unknown"),
        "reason": str(closure.get("reason") or "no reason recorded"),
        "closed": _stamp(closure.get("closed_at"), now),
        "was": str(closure.get("previous_status") or "not recorded"),
        "broker_lookup": str(closure.get("broker_lookup") or "not recorded"),
        "event_href": _event_href(closure.get("client_order_id")),
    }


def _event_href(client_order_id: object) -> str | None:
    """The system-events page narrowed to order closures, where the full audit event is."""

    if not client_order_id:
        return None
    return f"{PATHS['events']}?" + urlencode({"event_type": ORDER_CLOSED_EVENT})


def _event_row(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    detail = row.get("detail")
    event: dict[str, Any] = {
        "event_type": str(row.get("event_type", "")),
        "created": _stamp(row.get("created_at"), now),
        "correlation_id": _copy(row.get("correlation_id")),
        "change": None,
        "disconnect": None,
        "closure": _closure_view(row.get("closure"), now),
        "closed_order": _copy((row.get("closure") or {}).get("client_order_id")),
    }
    disconnect = row.get("disconnect")
    if disconnect is not None:
        kind = str(disconnect.get("kind"))
        event["disconnect"] = {
            "kind": kind,
            "label": _DISCONNECT_KINDS.get(kind, _DISCONNECT_KINDS["unknown"]),
            "note": str(disconnect.get("note") or ""),
        }
    if detail is not None:
        target = str(detail.get("to") or "unknown")
        checklist = detail.get("checklist") or ()
        event["change"] = {
            "text": f"{detail.get('from') or 'unknown'} to {target}",
            "status": _status(target, _KILL_SWITCH, "crit"),
            "actor": _ACTORS.get(str(detail.get("actor")), "Unknown"),
            "kind": "automatic" if detail.get("automatic") else "manual",
            "reason": str(detail.get("reason") or "no reason recorded"),
            "checklist": f"{len(checklist)} checklist item(s) confirmed" if checklist else "",
        }
    return event


def _risk(payload: Mapping[str, Any], now: datetime, query: HistoryQuery | None) -> dict[str, Any]:
    refusals = payload["refusals"]
    latest = refusals.get("latest_refusal")

    def gate(item: Mapping[str, Any]) -> dict[str, Any]:
        latest_at: Stamp | None = None
        if item.get("latest_at"):
            latest_at = _stamp(item["latest_at"], now)
        return {
            "position": item.get("position"),
            "label": str(item.get("label", "")),
            "gate": str(item.get("gate") or "none recorded"),
            "refusals": int(item.get("refusals", 0)),
            "latest": latest_at,
        }

    kill_switch = payload["kill_switch"]
    return {
        "state": _status(kill_switch.get("state"), _KILL_SWITCH, "crit"),
        "decisions": int(refusals.get("decisions", 0)),
        "approved": int(refusals.get("approved", 0)),
        "refused": int(refusals.get("refused", 0)),
        "latest": _decision_row(latest, now) if latest else None,
        "gates": [gate(item) for item in refusals.get("gates", ())],
        "other": [gate(item) for item in refusals.get("other", ())],
        "transitions_total": int(kill_switch.get("total", 0)),
        "refusals_href": _href(
            PATHS["risk_decisions"],
            (query.params() if query else {}) | {"outcome": "refused"},
        ),
        "transitions_href": _href(
            PATHS["events"],
            (query.params() if query else {}) | {"event_type": "kill_switch_transition"},
        ),
    }


_ROWS = {
    "orders": _order_row,
    "signals": _signal_row,
    "risk_decisions": _decision_row,
    "discrepancies": _discrepancy_row,
    "events": _event_row,
}
