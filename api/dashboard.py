"""Presentation model for the server-rendered operator dashboard.

``build_dashboard`` turns the ``/operator/state`` payload into display rows. It adds
no data and calls nothing: every value comes from that payload. Each value carries a
tone, a word, and its freshness, so unavailable, unknown, last-known, and zero never
look alike on the page.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from api.controls import REARM_CHECKLIST

# Colour is never the only carrier: every tone pairs a mark with a word.
MARKS = {"ok": "●", "warn": "▲", "crit": "■", "unknown": "○", "neutral": "◐"}
_SEVERITY = {"crit": 4, "warn": 3, "unknown": 2, "neutral": 1, "ok": 0}
ROW_LIMIT = 25

_KILL_SWITCH = {"running": "ok", "paused": "warn", "halted": "crit"}
_APPLICATION = {"healthy": "ok", "degraded": "warn"}
_BROKER = {"healthy": "ok", "unavailable": "crit", "not_configured": "unknown"}
_RECOVERY = {"reconciled": "ok", "halted": "crit", "no_broker": "neutral", "not_run": "unknown"}
_RECONCILIATION = {
    "clean": "ok",
    "diverged": "crit",
    "unavailable": "crit",
    "not_run": "unknown",
    "not_scheduled": "unknown",
}
_RUNTIME = {
    "running": "ok",
    "degraded": "warn",
    "failed": "crit",
    "halted": "crit",
    "stopped": "warn",
    "disabled": "neutral",
    "not_started": "unknown",
}
_CYCLE = {
    "no_signal": "ok",
    "submitted": "ok",
    "refused": "warn",
    "rejected": "warn",
    "broker_error": "warn",
    "ambiguous": "crit",
    "unresolved": "crit",
    "halted": "crit",
}
_HEARTBEAT = {
    "healthy": "ok",
    "starting": "neutral",
    "degraded": "warn",
    "unhealthy": "crit",
    "unknown": "unknown",
}
_ORDER = {
    "pending_submit": "warn",
    "unknown": "crit",
    "open": "neutral",
    "partially_filled": "neutral",
    "filled": "ok",
    "canceled": "neutral",
    "rejected": "crit",
}
_ALERT = {"critical": "crit", "warning": "warn"}
_DELIVERY = {"sent": "ok", "failed": "crit"}
_DESTINATIONS = (("phone_push", "Phone push"), ("email", "Email"))
_ROLE_WORDS = {"admin": "Administrator", "operator": "Operator"}
_WATCH_FEED = {"fresh": "ok", "stale": "warn", "unavailable": "crit", "not_collected": "unknown"}


@dataclass(frozen=True, slots=True)
class Status:
    word: str
    tone: str

    @property
    def mark(self) -> str:
        return MARKS[self.tone]


@dataclass(frozen=True, slots=True)
class Stamp:
    """A UTC time with its age at render time, or a word explaining its absence."""

    iso: str | None
    text: str
    age: str | None = None


@dataclass(frozen=True, slots=True)
class Fact:
    """One labelled value. ``kind`` keeps counts, text, times, and absences distinct."""

    label: str
    kind: str
    text: str = ""
    stamp: Stamp | None = None
    status: Status | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class Card:
    anchor: str
    label: str
    status: Status
    detail: str
    fresh_label: str
    fresh: Stamp | None = None


@dataclass(frozen=True, slots=True)
class Safety:
    status: Status
    summary: str
    causes: tuple[str, ...]
    next_step: str
    last_change: dict[str, Any] | None = None

    @property
    def halted(self) -> bool:
        return self.status.word == "halted"

    @property
    def running(self) -> bool:
        return self.status.word == "running"


def build_dashboard(
    snapshot: Mapping[str, Any], *, role: str, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    trading = snapshot.get("trading", {})
    mode = str(trading.get("mode", "unknown"))
    strategies = [_heartbeat(item, now) for item in snapshot.get("strategies", ())]
    alerts = _alerts(snapshot, now, is_admin=role == "admin")
    safety = _safety(snapshot, now)
    portfolio = _portfolio(snapshot, now)
    activity = _activity(snapshot, now)
    watch_feed = _watch_feed(snapshot, now)
    cards = _cards(snapshot, strategies, alerts, now)
    attention = _attention(snapshot, safety, portfolio, watch_feed, alerts)
    return {
        "header": {
            "mode": mode,
            "mode_tone": "crit" if mode == "live" else "accent",
            "credential_scope": str(trading.get("credential_scope", "unknown")),
            "strategy_version": str(trading.get("strategy_version", "unknown")),
            "role": "Administrator" if role == "admin" else "Operator",
            "is_admin": role == "admin",
            "updated": _stamp(snapshot.get("updated_at"), now),
        },
        "safety": safety,
        "clock": _clock(now),
        "kill_switch_states": [
            {
                "status": Status(word, _KILL_SWITCH[word]),
                "meaning": meaning,
                "current": word == safety.status.word,
            }
            for word, meaning in _KILL_SWITCH_MEANINGS
        ],
        "cards": cards,
        "checks": [card for card in cards if card.label not in {"Portfolio data", "P/L"}],
        "portfolio_cards": [card for card in cards if card.label in {"Portfolio data", "P/L"}],
        "attention": attention,
        "portfolio": portfolio,
        "activity": activity,
        "watch_feed": watch_feed,
        "alerts": alerts,
        "errors": _errors(snapshot, now),
        "destinations": _destinations(snapshot),
        "health": _health(snapshot, now),
        "strategies": strategies,
        "legend": [Status(word, tone) for tone, word in _LEGEND],
        "rearm_checklist": REARM_CHECKLIST,
        # Only an administrator can close an order, so only an administrator is shown the list.
        "unresolved": _unresolved(snapshot, now) if role == "admin" else None,
    }


def _unresolved(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """Saved orders the venue has not confirmed, for the close control.

    ``None`` when the payload carries no such list (it was not read), which is different from
    a list with nothing in it: only an empty list says no order is waiting.
    """

    payload = snapshot.get("unresolved_orders")
    if payload is None:
        return None
    if not payload.get("readable"):
        return {"readable": False, "rows": []}
    rows: list[dict[str, Any]] = []
    for order in payload.get("orders", ()):
        request = order.get("request", {})
        rows.append(
            {
                "client_order_id": str(request.get("client_order_id", "")),
                "symbol": str(request.get("symbol", "")),
                "side": str(request.get("side", "")),
                "quantity": _plain_amount(request.get("quantity", "")),
                "strategy_version": str(request.get("strategy_version", "")),
                "status": _status(order.get("status"), _ORDER),
                "created": _stamp(order.get("created_at"), now),
            }
        )
    return {"readable": True, "rows": rows}


_KILL_SWITCH_MEANINGS = (
    (
        "running",
        "Permits work, subject to every other risk gate. It does not prove health or trading.",
    ),
    ("paused", "New trading work is refused. Investigate before anyone re-arms."),
    (
        "halted",
        "A critical condition. Automatic processes cannot re-arm it, and PAUSE cannot "
        "lower it. Only an administrator re-arm, after the checklist.",
    ),
)
_LEGEND = (
    ("ok", "ok"),
    ("warn", "warning"),
    ("crit", "critical"),
    ("unknown", "unknown or unavailable"),
    ("neutral", "neutral: no health verdict"),
)


def _status(value: object, mapping: Mapping[str, str], default: str = "unknown") -> Status:
    word = str(value) if value not in (None, "") else "unknown"
    return Status(word, mapping.get(word, default))


def _stamp(value: object, now: datetime, *, missing: str = "not recorded") -> Stamp:
    if not value:
        return Stamp(None, missing)
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return Stamp(None, "unreadable time")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    return Stamp(
        moment.isoformat(),
        moment.strftime("%Y-%m-%d %H:%M:%S UTC"),
        _age(now - moment),
    )


def _clock(now: datetime) -> dict[str, Any]:
    """The server's UTC time at render, in the page's stamp format, as the clock's anchor."""

    moment = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return {
        "iso": moment.isoformat(),
        "text": moment.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "epoch_ms": int(moment.timestamp() * 1000),
    }


def _age(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} s ago"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min ago"
    return f"{hours // 24} d {hours % 24} h ago"


def _count(label: str, value: object, *, missing: str = "not recorded") -> Fact:
    if isinstance(value, int) and not isinstance(value, bool):
        return Fact(label, "count", str(value))
    return Fact(label, "absent", missing)


def _text(label: str, value: object, *, missing: str = "not recorded") -> Fact:
    if value in (None, ""):
        return Fact(label, "absent", missing)
    return Fact(label, "text", str(value))


def _time(label: str, value: object, now: datetime, *, missing: str = "not recorded") -> Fact:
    stamp = _stamp(value, now, missing=missing)
    return Fact(label, "stamp" if stamp.iso else "absent", stamp.text, stamp=stamp)


def _state(label: str, status: Status) -> Fact:
    return Fact(label, "status", status.word, status=status)


def _safety(snapshot: Mapping[str, Any], now: datetime) -> Safety:
    status = _status(snapshot.get("risk", {}).get("kill_switch"), _KILL_SWITCH, "crit")
    if status.word == "running":
        return Safety(
            status,
            "Work is permitted, subject to every other risk gate. Running does not mean "
            "the system is healthy or trading.",
            (),
            "Keep monitoring. PAUSE to investigate; EMERGENCY STOP when continuing may be unsafe.",
        )
    if status.word == "paused":
        return Safety(
            status,
            "New trading work is refused.",
            _causes(snapshot),
            "Investigate the cause. Only an administrator re-arm resumes trading, after "
            "the checklist.",
            _last_change(snapshot, status.word, now),
        )
    return Safety(
        status,
        "New trading work is refused, and only an administrator can re-arm.",
        _causes(snapshot),
        "Leave it halted and escalate. An administrator re-arms only after the checklist.",
        _last_change(snapshot, status.word, now),
    )


_ACTORS = {"operator": "Operator", "admin": "Administrator", "system": "The system"}


def _transition(item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    source = str(item.get("from", "unknown"))
    target = str(item.get("to", "unknown"))
    return {
        "change": f"{source} to {target}",
        "status": _status(target, _KILL_SWITCH, "crit"),
        "actor": _ACTORS.get(str(item.get("actor")), "Unknown"),
        "kind": "automatic" if item.get("automatic") else "manual",
        "reason": str(item.get("reason") or "no reason recorded"),
        "at": _stamp(item.get("created_at"), now),
    }


def _last_change(snapshot: Mapping[str, Any], state: str, now: datetime) -> dict[str, Any] | None:
    """The recorded transition into the current state, when the newest record explains it."""

    transitions = snapshot.get("risk", {}).get("transitions") or ()
    if not transitions or transitions[0].get("to") != state:
        return None
    return _transition(transitions[0], now)


def _causes(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Explain a pause or halt from the state that can trip the switch.

    The recorded transition, shown beside these, says who or what set the state.
    """

    causes = []
    recovery = snapshot.get("recovery", {})
    if recovery.get("status") == "halted":
        causes.append(f"Startup recovery halted: {recovery.get('detail', 'no detail recorded')}.")
    reconciliation = snapshot.get("reconciliation", {})
    if reconciliation.get("last_result") == "diverged":
        count = reconciliation.get("last_discrepancies", "an unknown number of")
        causes.append(f"Scheduled reconciliation diverged: {count} difference(s) with the broker.")
    elif reconciliation.get("last_result") == "unavailable":
        causes.append("Scheduled reconciliation could not read the broker.")
    runtime = snapshot.get("runtime", {})
    if runtime.get("status") in {"failed", "halted"}:
        causes.append(
            f"Paper runtime {runtime.get('status')}: {runtime.get('detail', 'no detail')}."
        )
    return tuple(causes)


def _cards(
    snapshot: Mapping[str, Any],
    strategies: Sequence[Mapping[str, Any]],
    alerts: Mapping[str, Any],
    now: datetime,
) -> list[Card]:
    application = snapshot.get("application", {})
    connectivity = snapshot.get("connectivity", {})
    runtime = snapshot.get("runtime", {})
    recovery = snapshot.get("recovery", {})
    reconciliation = snapshot.get("reconciliation", {})
    portfolio = _portfolio(snapshot, now)
    pnl = snapshot.get("portfolio", {}).get("pnl", {})
    pnl_at = _stamp(pnl.get("as_of"), now) if pnl.get("as_of") else None
    pnl_detail = str(pnl.get("detail", "no detail"))

    cycle = runtime.get("last_cycle_status")
    cycle_label = f"Last cycle {cycle}" if cycle else "No strategy cycle has run"
    cycle_at = _stamp(runtime.get("last_cycle_at"), now) if cycle else None

    worst = _worst(strategies)
    if worst is None:
        strategy_card = Card(
            "health",
            "Strategy",
            Status("none registered", "unknown"),
            "No strategy heartbeat is registered.",
            "Heartbeat never received",
        )
    else:
        extra = len(strategies) - 1
        detail = f"{worst['name']} ({worst['version']}): {worst['detail']}"
        if extra:
            detail += f"; {extra} more in System health"
        seen = worst["last_seen"]
        strategy_card = Card(
            "health",
            "Strategy",
            worst["status"],
            detail,
            "Last seen" if seen.iso else "Heartbeat never received",
            seen if seen.iso else None,
        )

    runs = reconciliation.get("runs")
    reconciliation_detail = (
        f"{reconciliation.get('last_discrepancies', 0)} difference(s) in the last run; "
        f"{runs} run(s) since start."
        if isinstance(runs, int)
        else "No scheduled reconciliation is running in this process."
    )
    recovery_done = _stamp(recovery.get("completed_at"), now)
    last_run = _stamp(reconciliation.get("last_run_at"), now)

    return [
        Card(
            "health",
            "Application",
            _status(application.get("status"), _APPLICATION),
            "Follows the broker refresh. /health only proves the web process responds.",
            "Heartbeat",
            _stamp(application.get("heartbeat"), now),
        ),
        Card(
            "health",
            "Broker",
            _status(connectivity.get("status"), _BROKER),
            str(connectivity.get("detail", "")),
            "Checked",
            _stamp(connectivity.get("checked_at"), now),
        ),
        Card(
            "health",
            "Paper runtime",
            _status(runtime.get("status"), _RUNTIME),
            str(runtime.get("detail", "")),
            cycle_label,
            cycle_at if cycle_at and cycle_at.iso else None,
        ),
        strategy_card,
        Card(
            "health",
            "Startup recovery",
            _status(recovery.get("status"), _RECOVERY),
            str(recovery.get("detail", "")),
            "Completed" if recovery_done.iso else "Not completed in this process",
            recovery_done if recovery_done.iso else None,
        ),
        Card(
            "health",
            "Reconciliation",
            _status(reconciliation.get("last_result"), _RECONCILIATION),
            reconciliation_detail,
            "Last run" if last_run.iso else "No run recorded",
            last_run if last_run.iso else None,
        ),
        Card(
            "portfolio",
            "Portfolio data",
            portfolio["status"],
            portfolio["detail"],
            "Freshness only, not correctness",
        ),
        Card(
            "portfolio",
            "P/L",
            _status(pnl.get("status"), {"unavailable": "unknown"}, "neutral"),
            pnl_detail if pnl_at else f"{pnl_detail}. Unavailable is not zero.",
            "Latest equity snapshot" if pnl_at else "No figure",
            pnl_at,
        ),
        Card(
            "alerts",
            "Alerts",
            alerts["summary"],
            alerts["summary_detail"],
            "Latest" if alerts["latest"] else "None recorded since start",
            alerts["latest"],
        ),
    ]


def _worst(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: _SEVERITY[row["status"].tone])


def _portfolio(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    portfolio = snapshot.get("portfolio", {})
    broker = snapshot.get("connectivity", {}).get("status")
    raw_balances = list(portfolio.get("balances", ()))
    raw_positions = list(portfolio.get("positions", ()))
    status_word = portfolio.get("status")
    last_known = status_word != "current" and bool(raw_balances or raw_positions)
    if status_word == "current":
        status = Status("current", "neutral")
    elif last_known:
        status = Status("last known", "warn")
    else:
        status = _status(status_word, {}, "unknown")

    if status_word == "current":
        empty_reason = "reported by the broker"
    elif broker == "not_configured":
        empty_reason = "Unavailable: no broker is configured"
    else:
        empty_reason = "Unavailable: the broker could not be read"

    balances = [
        {
            "asset": item.get("asset", ""),
            "available": item.get("available", ""),
            "hold": item.get("hold", ""),
            "as_of": _stamp(item.get("as_of"), now),
        }
        for item in raw_balances
    ]
    positions = [
        {
            "symbol": item.get("symbol", ""),
            "quantity": item.get("quantity", ""),
            "average_price": _average_price(item.get("average_price"), item.get("cost_from_fills")),
            "as_of": _stamp(item.get("as_of"), now),
        }
        for item in raw_positions
    ]
    return {
        "status": status,
        "detail": str(portfolio.get("detail", "")),
        "source": "broker snapshot"
        if status_word == "current"
        else "last-known broker snapshot"
        if last_known
        else "unavailable",
        "current": status_word == "current",
        "last_known": last_known,
        "empty_reason": empty_reason,
        "balances": balances,
        "positions": positions,
        "pnl": _status(
            portfolio.get("pnl", {}).get("status"), {"unavailable": "unknown", "current": "neutral"}
        ),
        "pnl_detail": str(portfolio.get("pnl", {}).get("detail", "")),
    }


def _plain_amount(value: object) -> str:
    """An amount as it was placed. The database keeps 18 decimals, which read as noise."""

    try:
        return format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation:
        return str(value)


def _average_price(value: object, from_fills: object = None) -> Fact:
    # None means the venue reports no cost basis; a real 0 (an airdrop) is a known price.
    try:
        known = value not in (None, "") and Decimal(str(value)) >= 0
    except InvalidOperation:
        known = False
    if known:
        return Fact("Average price", "text", str(value))  # a venue-reported price always wins
    if not isinstance(from_fills, Mapping):
        return Fact("Average price", "absent", "not known")
    derived = from_fills.get("average_price")
    try:
        derived_known = derived not in (None, "") and Decimal(str(derived)) >= 0
    except InvalidOperation:
        derived_known = False
    if derived_known:
        return Fact("Average price", "text", _plain_amount(derived), note="from our fills")
    reason = from_fills.get("reason")
    return Fact("Average price", "absent", "not known", note=str(reason) if reason else "")


def _activity(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    orders = list(snapshot.get("orders", ()))
    fills = list(snapshot.get("fills", ()))
    signals = list(snapshot.get("signals", ()))
    order_rows = [
        {
            "client_order_id": order.get("request", {}).get("client_order_id", ""),
            "href": _history_order_href(order.get("request", {}).get("client_order_id")),
            "symbol": order.get("request", {}).get("symbol", ""),
            "side": order.get("request", {}).get("side", ""),
            "order_type": order.get("request", {}).get("order_type", ""),
            "quantity": order.get("request", {}).get("quantity", ""),
            "filled": order.get("filled_quantity", ""),
            "status": _status(order.get("status"), _ORDER),
            "updated": _stamp(order.get("updated_at"), now),
            "sort": str(order.get("updated_at", "")),
        }
        for order in orders
    ]
    fill_rows = [
        {
            "fill_id": fill.get("fill_id", ""),
            "symbol": fill.get("symbol", ""),
            "side": fill.get("side", ""),
            "quantity": fill.get("quantity", ""),
            "price": fill.get("price", ""),
            "fee": fill.get("fee", ""),
            "fee_asset": fill.get("fee_asset", ""),
            "occurred": _stamp(fill.get("occurred_at"), now),
            "sort": str(fill.get("occurred_at", "")),
        }
        for fill in fills
    ]
    signal_rows = [
        {
            "signal_id": signal.get("signal_id", ""),
            "symbol": signal.get("symbol", ""),
            "side": signal.get("side", ""),
            "quantity": signal.get("quantity", ""),
            "strategy_version": signal.get("strategy_version", ""),
            "created": _stamp(signal.get("created_at"), now),
            "sort": str(signal.get("created_at", "")),
        }
        for signal in signals
    ]
    return {
        "available": snapshot.get("readable") is not False
        and bool(snapshot.get("updated_at"))
        and all(key in snapshot for key in ("orders", "fills", "signals")),
        "source": "process-local operator snapshot",
        "counts": {"signals": len(signals), "orders": len(orders), "fills": len(fills)},
        "orders": _newest(order_rows),
        "fills": _newest(fill_rows),
        "signals": _newest(signal_rows),
        "limit": ROW_LIMIT,
    }


def _history_order_href(client_order_id: object) -> str:
    if not client_order_id:
        return "/operator/history/orders"
    return f"/operator/history/orders/{quote(str(client_order_id), safe='')}"


def _watch_feed(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    payload = snapshot.get("watch_feed")
    if not isinstance(payload, Mapping):
        return {
            "available": False,
            "enabled": None,
            "status": Status("unavailable", "unknown"),
            "detail": "The watch-only feed read model is unavailable.",
            "rows": [],
        }

    enabled = payload.get("enabled")
    rows: list[dict[str, Any]] = []
    for item in payload.get("symbols", ()):
        state = str(item.get("state", "not_collected"))
        rows.append(
            {
                "symbol": str(item.get("symbol", "")),
                "status": _status(state, _WATCH_FEED),
                "detail": str(item.get("detail") or ""),
                "last_candle": _stamp(item.get("last_candle_at"), now, missing="none collected"),
            }
        )

    if enabled is False:
        status = Status("disabled", "neutral")
        detail = "Watch-only collection is disabled. The trading feed is separate."
    elif enabled is not True:
        status = Status("unavailable", "unknown")
        detail = "The watch-only feed state was not recorded."
    elif rows:
        status = max(rows, key=lambda row: _SEVERITY[row["status"].tone])["status"]
        detail = "Per-symbol freshness for the watch-only feed; this is separate from trading data."
    else:
        status = Status("no symbols", "neutral")
        detail = "No watch-only symbols are being collected. The trading feed is separate."
    return {
        "available": True,
        "enabled": enabled,
        "status": status,
        "detail": detail,
        "rows": rows[:ROW_LIMIT],
        "total": len(rows),
        "limit": ROW_LIMIT,
    }


def _attention(
    snapshot: Mapping[str, Any],
    safety: Safety,
    portfolio: Mapping[str, Any],
    watch_feed: Mapping[str, Any],
    alerts: Mapping[str, Any],
) -> dict[str, Any]:
    """Rank actionable conditions using only the bounded operator snapshot."""

    required = (
        "updated_at",
        "risk",
        "connectivity",
        "recovery",
        "reconciliation",
        "runtime",
        "watch_feed",
        "portfolio",
        "alerts",
        "orders",
        "fills",
        "signals",
    )
    missing = [key for key in required if key not in snapshot]
    if snapshot.get("readable") is False or missing:
        detail = (
            "The operator read model is unavailable. No incident or empty state can be confirmed."
            if snapshot.get("readable") is False
            else "The operator read model is incomplete; missing " + ", ".join(missing) + "."
        )
        return {
            "available": False,
            "status": Status("read model unavailable", "unknown"),
            "detail": detail,
            "rows": [],
            "limit": ROW_LIMIT,
        }

    rows: list[dict[str, Any]] = []

    def add(
        title: str,
        status: Status,
        detail: str,
        next_step: str,
        href: str,
    ) -> None:
        rows.append(
            {
                "title": title,
                "status": status,
                "detail": detail,
                "next_step": next_step,
                "href": href,
            }
        )

    if not safety.running:
        if safety.causes:
            cause = " ".join(safety.causes)
        elif safety.last_change:
            change = safety.last_change
            cause = (
                f"Recorded change: {change['change']} by {change['actor']} "
                f"({change['kind']}); {change['reason']}."
            )
        else:
            cause = (
                "Cause unavailable: no recorded safety cause explains this state "
                "in the current snapshot."
            )
        add(
            f"Kill switch {safety.status.word}",
            safety.status,
            cause,
            safety.next_step,
            "#safety",
        )

    unresolved = snapshot.get("unresolved_orders")
    if unresolved is None:
        add(
            "Saved unresolved orders were not checked",
            Status("unavailable", "unknown"),
            "This view cannot confirm whether a persisted order is waiting on the venue.",
            "Open order history and investigate any pending or unknown order by client order ID.",
            "/operator/history/orders",
        )
    elif not unresolved.get("readable"):
        add(
            "Saved unresolved orders could not be read",
            Status("unreadable", "crit"),
            "No pending or unknown persisted order can be confirmed resolved from this snapshot.",
            "Check the order history and application logs before allowing new entries.",
            "/operator/history/orders",
        )

    seen_order_ids: set[str] = set()
    order_sources = [snapshot.get("orders", ())]
    if isinstance(unresolved, Mapping) and unresolved.get("readable"):
        order_sources.append(unresolved.get("orders", ()))
    for source in order_sources:
        for order in source:
            status_word = str(order.get("status", "unknown"))
            if status_word not in {"pending_submit", "unknown"}:
                continue
            request = order.get("request", {})
            client_order_id = str(request.get("client_order_id") or "")
            if client_order_id and client_order_id in seen_order_ids:
                continue
            if client_order_id:
                seen_order_ids.add(client_order_id)
            status = _status(status_word, _ORDER)
            symbol = str(request.get("symbol", ""))
            detail = (
                f"{symbol + ': ' if symbol else ''}{status_word}. The venue state is unresolved; "
                "this order may already exist."
            )
            href = _history_order_href(client_order_id)
            add(
                "Unknown order" if status_word == "unknown" else "Pending submission order",
                status,
                detail,
                "Look it up by client order ID. Never resubmit it.",
                href,
            )

    reconciliation = snapshot.get("reconciliation", {})
    result = str(reconciliation.get("last_result", ""))
    if result in {"diverged", "unavailable"}:
        status = _status(result, _RECONCILIATION)
        count = reconciliation.get("last_discrepancies")
        detail = (
            f"The last scheduled run found {count} difference(s) with the broker."
            if result == "diverged" and isinstance(count, int)
            else "The last scheduled reconciliation could not read the broker."
        )
        add(
            "Reconciliation needs review",
            status,
            detail,
            "Review the recorded discrepancy and reconciliation history before resuming entries.",
            "/operator/history/discrepancies",
        )

    if watch_feed.get("enabled") is True:
        for item in watch_feed.get("rows", ()):
            word = item["status"].word
            if word not in {"stale", "unavailable", "not_collected"}:
                continue
            freshness = (
                f" Last candle: {item['last_candle'].text}." if item["last_candle"].iso else ""
            )
            detail = (item["detail"] or f"Watch-only data is {word}.") + freshness
            display_word = "not collected" if word == "not_collected" else word
            add(
                f"Watch-only data {display_word}: {item['symbol']}",
                item["status"],
                detail,
                "Inspect this symbol's chart and source in Watch Coins. "
                "This feed does not drive trading.",
                "/operator/watchlist",
            )

    if portfolio.get("last_known"):
        add(
            "Portfolio is last known",
            Status("last known", "warn"),
            "The displayed balances and positions are retained for diagnosis and may not "
            "match the broker.",
            "Refresh the broker-backed portfolio and verify its source and as-of times.",
            "#portfolio",
        )
    elif portfolio.get("status", Status("unavailable", "unknown")).tone == "unknown":
        add(
            "Portfolio data unavailable",
            portfolio["status"],
            str(portfolio.get("empty_reason", "The portfolio read model is unavailable.")),
            "Check broker connectivity and portfolio freshness in Operational checks.",
            "#portfolio",
        )

    for alert in alerts.get("rows", ()):
        failed = any(item["status"].word == "failed" for item in alert["deliveries"])
        if alert["severity"].tone not in {"warn", "crit"} and not failed:
            continue
        status = alert["severity"] if not failed else Status("delivery failed", "warn")
        add(
            f"Alert: {alert['condition'] or 'condition not recorded'}",
            status,
            alert["message"] or "No alert detail was recorded.",
            "Review the alert and delivery result below. Dismissing an alert records review; "
            "it does not resolve the condition.",
            "#alerts",
        )

    rows.sort(key=lambda row: _SEVERITY[row["status"].tone], reverse=True)
    return {
        "available": True,
        "status": Status("attention needed", "warn")
        if rows
        else Status("no active incident recorded", "ok"),
        "detail": ""
        if rows
        else "No active incident is recorded in the available snapshot. Review unknown and "
        "unavailable checks below; this is not a readiness verdict.",
        "rows": rows[:ROW_LIMIT],
        "total": len(rows),
        "limit": ROW_LIMIT,
    }


def _newest(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: row["sort"], reverse=True)[:ROW_LIMIT]


def _alerts(
    snapshot: Mapping[str, Any], now: datetime, *, is_admin: bool = False
) -> dict[str, Any]:
    configured = set(snapshot.get("alert_destinations", ()))
    rows: list[dict[str, Any]] = []
    for index, alert in enumerate(snapshot.get("alerts", ())):
        deliveries = [
            {
                "destination": _destination_label(item.get("destination")),
                "status": _status(item.get("status"), _DELIVERY),
            }
            for item in alert.get("deliveries", ())
        ]
        dismissed = "dismissed_at" in alert
        rows.append(
            {
                "severity": _status(alert.get("severity"), _ALERT, "neutral"),
                "condition": str(alert.get("condition", "")),
                "message": str(alert.get("message", "")),
                "created": _stamp(alert.get("created_at"), now),
                "deliveries": deliveries,
                "undelivered": "" if deliveries else _undelivered(configured),
                "order": index,
                "dismissed": dismissed,
                "dismissed_at": _stamp(alert.get("dismissed_at"), now) if dismissed else None,
                "dismissed_by": _ROLE_WORDS.get(str(alert.get("dismissed_by")), "Operator")
                if dismissed
                else "",
            }
        )
    total = len(rows)
    new = [row for row in rows if not row["dismissed"]]
    dismissed_rows = [row for row in rows if row["dismissed"]]
    critical = sum(1 for row in new if row["severity"].tone == "crit")
    warnings = sum(1 for row in new if row["severity"].tone == "warn")
    failed = sum(1 for row in new for item in row["deliveries"] if item["status"].word == "failed")
    # A failed delivery means someone may not have been told, so it is never neutral.
    if critical:
        summary = Status(f"{critical} critical", "crit")
    elif warnings:
        summary = Status(f"{warnings} warning", "warn")
    elif failed:
        summary = Status("delivery failed", "warn")
    elif dismissed_rows and not new:
        summary = Status("0 new", "neutral")
    else:
        summary = Status(f"{len(new)} recorded", "neutral")
    if dismissed_rows:
        notes = [
            f"{len(new)} new alert(s); {len(dismissed_rows)} dismissed, since this process started."
        ]
    else:
        notes = [f"{total} alert(s) since this process started."]
    if failed:
        notes.append(f"{failed} delivery attempt(s) failed.")
    if not configured:
        notes.append("No destination is configured, so nobody is notified.")
    newest = _newest_events(rows)
    return {
        "rows": _newest_events(new)[:ROW_LIMIT],
        "dismissed_rows": _newest_events(dismissed_rows)[:ROW_LIMIT],
        "total": total,
        "new_total": len(new),
        "dismissed_total": len(dismissed_rows),
        # What the page showed, sent back so an alert that arrives later stays new.
        "through": total,
        "can_dismiss": is_admin and bool(new),
        "summary": summary,
        "summary_detail": " ".join(notes),
        "latest": newest[0]["created"] if newest and newest[0]["created"].iso else None,
        "limit": ROW_LIMIT,
    }


def _undelivered(configured: set[str]) -> str:
    if not configured:
        return "Not delivered: no destinations are configured."
    return "No delivery was recorded."


def _newest_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Untimed events sort as oldest; equal times keep their recorded order.
    return sorted(rows, key=lambda row: (row["created"].iso or "", row["order"]), reverse=True)


def _errors(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    rows: list[dict[str, Any]] = [
        {
            "condition": str(error.get("condition", "")),
            "message": str(error.get("message", "")),
            "created": _stamp(error.get("created_at"), now),
            "order": index,
        }
        for index, error in enumerate(snapshot.get("errors", ()))
    ]
    newest = _newest_events(rows)
    return {"rows": newest[:ROW_LIMIT], "total": len(rows), "limit": ROW_LIMIT}


def _destination_label(value: object) -> str:
    return dict(_DESTINATIONS).get(str(value), "Other destination")


def _destinations(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    # Names only: recipients, topic URLs, and tokens never reach this payload.
    configured = set(snapshot.get("alert_destinations", ()))
    return [
        {
            "label": label,
            "status": Status("configured", "ok")
            if key in configured
            else Status("not configured", "unknown"),
        }
        for key, label in _DESTINATIONS
    ]


def _heartbeat(item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "name": str(item.get("name", "")),
        "version": str(item.get("version", "unknown")),
        "status": _status(item.get("status"), _HEARTBEAT),
        "detail": str(item.get("detail", "")),
        "last_seen": _stamp(item.get("last_seen"), now, missing="never"),
    }


def _health(snapshot: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    application = snapshot.get("application", {})
    trading = snapshot.get("trading", {})
    connectivity = snapshot.get("connectivity", {})
    recovery = snapshot.get("recovery", {})
    reconciliation = snapshot.get("reconciliation", {})
    runtime = snapshot.get("runtime", {})
    not_scheduled = "not scheduled"
    cycle = runtime.get("last_cycle_status")
    return [
        {
            "title": "Application",
            "facts": [
                _state("Status", _status(application.get("status"), _APPLICATION)),
                _time("Heartbeat", application.get("heartbeat"), now),
                _text("Application version", application.get("version")),
                _text("Trading mode", trading.get("mode")),
                _text("Credential scope", trading.get("credential_scope")),
                _text("Strategy version", trading.get("strategy_version")),
                _time("Snapshot updated", snapshot.get("updated_at"), now),
            ],
        },
        {
            "title": "Broker",
            "facts": [
                _state("Status", _status(connectivity.get("status"), _BROKER)),
                _text("Detail", connectivity.get("detail")),
                _time("Checked", connectivity.get("checked_at"), now),
            ],
        },
        {
            "title": "Startup recovery",
            "facts": [
                _state("Status", _status(recovery.get("status"), _RECOVERY)),
                _text("Detail", recovery.get("detail")),
                _count("Pending orders", recovery.get("pending_orders")),
                _count("Recovered orders", recovery.get("recovered_orders")),
                _count("Discrepancies", recovery.get("discrepancies")),
                _time("Completed", recovery.get("completed_at"), now, missing="not completed"),
            ],
        },
        {
            "title": "Scheduled reconciliation",
            "facts": [
                _state("Last result", _status(reconciliation.get("last_result"), _RECONCILIATION)),
                _count(
                    "Differences in last run",
                    reconciliation.get("last_discrepancies"),
                    missing=not_scheduled,
                ),
                _count("Runs", reconciliation.get("runs"), missing=not_scheduled),
                _count("Clean runs", reconciliation.get("clean_runs"), missing=not_scheduled),
                _count("Diverged runs", reconciliation.get("diverged_runs"), missing=not_scheduled),
                _count(
                    "Unavailable runs",
                    reconciliation.get("unavailable_runs"),
                    missing=not_scheduled,
                ),
                _time("Last run", reconciliation.get("last_run_at"), now, missing="no run yet"),
                _time(
                    "Scheduler started",
                    reconciliation.get("started_at"),
                    now,
                    missing=not_scheduled,
                ),
            ],
        },
        {
            "title": "Paper runtime",
            "facts": [
                _state("Status", _status(runtime.get("status"), _RUNTIME)),
                _text("Detail", runtime.get("detail")),
                _state("Last cycle", _status(cycle, _CYCLE))
                if cycle
                else Fact("Last cycle", "absent", "no cycle has run"),
                _time(
                    "Last cycle at", runtime.get("last_cycle_at"), now, missing="no cycle has run"
                ),
            ],
        },
    ]


def build_rearm_review(
    snapshot: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Everything an administrator must see before re-arming, from ``/operator/state``."""

    now = now or datetime.now(UTC)
    warnings = _rearm_warnings(snapshot, now)
    safety = _safety(snapshot, now)
    return {
        "safety": safety,
        "warnings": warnings,
        "attention": sum(1 for item in warnings if item["status"].tone != "ok"),
        "transitions": [
            _transition(item, now) for item in snapshot.get("risk", {}).get("transitions") or ()
        ],
    }


def _orders_detail(pending: int, unknown: int, total: int, *, saved: bool, readable: bool) -> str:
    if not readable:
        return (
            "The saved orders could not be read, so none can be confirmed resolved. "
            "Do not confirm the orders item."
        )
    held = "saved" if saved else "this process holds"
    text = (
        f"{pending} pending submission and {unknown} unknown among the {total} order(s) {held}. "
        "Resolve each by looking it up with its client order ID. Never resubmit it."
    )
    if pending or unknown:
        text += (
            " An order the venue never received is closed from the dashboard, after the app "
            "asks the venue about it again."
        )
    return text


def _rearm_warnings(snapshot: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    recovery = snapshot.get("recovery", {})
    reconciliation = snapshot.get("reconciliation", {})
    runs = reconciliation.get("runs")
    # The saved order store is the source when the route read it. The snapshot's own order
    # list holds only what this process was handed, which the running service never is.
    saved = snapshot.get("unresolved_orders")
    if saved is not None and not saved.get("readable"):
        orders: list[str] = []
        order_status = Status("unreadable", "crit")
    else:
        source = saved.get("orders", ()) if saved is not None else snapshot.get("orders", ())
        orders = [str(order.get("status")) for order in source]
        order_status = Status("none", "ok")
    pending = orders.count("pending_submit")
    unknown = orders.count("unknown")
    if unknown:
        order_status = Status(f"{unknown} unknown", "crit")
    elif pending:
        order_status = Status(f"{pending} pending", "warn")
    strategies = [_heartbeat(item, now) for item in snapshot.get("strategies", ())]
    worst = _worst(strategies)
    if worst is None:
        strategy = {
            "label": "Strategy heartbeat",
            "status": Status("none registered", "unknown"),
            "detail": "No strategy heartbeat is registered.",
            "stamp": None,
        }
    else:
        strategy = {
            "label": "Strategy heartbeat",
            "status": worst["status"],
            "detail": f"{worst['name']} ({worst['version']}): {worst['detail']}",
            "stamp": worst["last_seen"] if worst["last_seen"].iso else None,
        }
    completed = _stamp(recovery.get("completed_at"), now)
    last_run = _stamp(reconciliation.get("last_run_at"), now)
    return [
        {
            "label": "Startup recovery",
            "status": _status(recovery.get("status"), _RECOVERY),
            "detail": str(recovery.get("detail", "")),
            "stamp": completed if completed.iso else None,
        },
        {
            "label": "Reconciliation",
            "status": _status(reconciliation.get("last_result"), _RECONCILIATION),
            "detail": (
                f"{reconciliation.get('last_discrepancies', 0)} difference(s) in the last run; "
                f"{runs} run(s) since start."
                if isinstance(runs, int)
                else "No scheduled reconciliation is running in this process."
            ),
            "stamp": last_run if last_run.iso else None,
        },
        {
            "label": "Pending or unknown orders",
            "status": order_status,
            "detail": _orders_detail(
                pending,
                unknown,
                len(orders),
                saved=saved is not None,
                readable=order_status.word != "unreadable",
            ),
            "stamp": None,
        },
        strategy,
    ]
