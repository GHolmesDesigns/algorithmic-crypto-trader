"""Provider-neutral state assembly for the authenticated operator surface."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from brokers.http import log_provider_failure
from brokers.interface import BrokerInterface
from core.guards import StartupSettings
from core.models import Balance, Fill, Order, Position, Signal, utc_now
from core.version import application_version
from risk.kill_switch import KillSwitch

from api.alerts import Alert, AlertDelivery, AlertRouter
from api.system_events import ALERTS_DISMISSED_EVENT, SystemEventJournal

logger = logging.getLogger(__name__)

TRANSITION_LIMIT = 10


@dataclass(frozen=True, slots=True)
class StrategyHeartbeat:
    name: str
    version: str
    status: str = "unknown"
    last_seen: datetime | None = None
    detail: str = "heartbeat has not been received"


@dataclass(frozen=True, slots=True)
class OperatorSnapshot:
    connectivity_status: str = "not_configured"
    connectivity_detail: str = "no broker is configured"
    connectivity_checked_at: datetime | None = None
    portfolio_status: str = "unavailable"
    portfolio_detail: str = "portfolio data is unavailable"
    balances: tuple[Balance, ...] = ()
    positions: tuple[Position, ...] = ()
    pnl_status: str = "unavailable"
    pnl_detail: str = "valuation data is unavailable"
    orders: tuple[Order, ...] = ()
    fills: tuple[Fill, ...] = ()
    signals: tuple[Signal, ...] = ()
    strategy_version: str = "unknown"
    strategy_heartbeats: tuple[StrategyHeartbeat, ...] = ()
    runtime_status: str = "not_started"
    runtime_detail: str = "paper runtime has not started"
    runtime_last_cycle_at: datetime | None = None
    runtime_last_cycle_status: str | None = None
    alerts: tuple[dict[str, Any], ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    updated_at: datetime = field(default_factory=utc_now)


class OperatorState:
    """Owns the last known operator view and its refresh semantics."""

    def __init__(
        self,
        *,
        settings: StartupSettings,
        kill_switch: KillSwitch,
        broker: BrokerInterface | None = None,
        alert_router: AlertRouter | None = None,
        strategy_version: str = "unknown",
    ) -> None:
        self.settings = settings
        self.kill_switch = kill_switch
        self.broker = broker
        self.alert_router = alert_router or AlertRouter()
        self.startup_recovery: Any = None
        self.scheduled_reconciliation: Any = None
        self.watch_feed: Any = None
        strategy = StrategyHeartbeat(name="primary", version=strategy_version)
        self.snapshot = OperatorSnapshot(
            strategy_version=strategy_version,
            strategy_heartbeats=(strategy,),
        )

    def register_strategy(self, name: str, version: str) -> None:
        heartbeats = [item for item in self.snapshot.strategy_heartbeats if item.name != name]
        heartbeats.append(StrategyHeartbeat(name=name, version=version))
        self.snapshot = replace(self.snapshot, strategy_heartbeats=tuple(heartbeats))

    def heartbeat(self, name: str, *, status: str = "healthy", detail: str = "ok") -> None:
        heartbeats = []
        found = False
        for item in self.snapshot.strategy_heartbeats:
            if item.name == name:
                heartbeats.append(replace(item, status=status, last_seen=utc_now(), detail=detail))
                found = True
            else:
                heartbeats.append(item)
        if not found:
            heartbeats.append(
                StrategyHeartbeat(
                    name=name,
                    version="unknown",
                    status=status,
                    last_seen=utc_now(),
                    detail=detail,
                )
            )
        self.snapshot = replace(self.snapshot, strategy_heartbeats=tuple(heartbeats))

    def set_local_data(
        self,
        *,
        orders: tuple[Order, ...] = (),
        fills: tuple[Fill, ...] = (),
        signals: tuple[Signal, ...] = (),
        strategy_version: str | None = None,
    ) -> None:
        self.snapshot = replace(
            self.snapshot,
            orders=orders,
            fills=fills,
            signals=signals,
            strategy_version=strategy_version or self.snapshot.strategy_version,
            updated_at=utc_now(),
        )

    def set_runtime(
        self,
        status: str,
        detail: str,
        *,
        cycle_status: str | None = None,
        cycle_at: datetime | None = None,
    ) -> None:
        self.snapshot = replace(
            self.snapshot,
            runtime_status=status,
            runtime_detail=detail,
            runtime_last_cycle_status=cycle_status,
            runtime_last_cycle_at=cycle_at,
            updated_at=utc_now(),
        )

    async def refresh(self) -> OperatorSnapshot:
        now = utc_now()
        if self.broker is None:
            self.snapshot = replace(
                self.snapshot,
                connectivity_status="not_configured",
                connectivity_detail="no broker is configured",
                connectivity_checked_at=now,
                portfolio_status="unavailable",
                portfolio_detail="broker portfolio data is unavailable",
                updated_at=now,
            )
            return self.snapshot

        try:
            balances = await self.broker.get_balances()
            positions = await self.broker.get_positions()
        except Exception as exc:
            log_provider_failure(logger, "operator broker refresh", exc)
            self.snapshot = replace(
                self.snapshot,
                connectivity_status="unavailable",
                connectivity_detail="broker is unavailable; portfolio data is not current",
                connectivity_checked_at=now,
                portfolio_status="unavailable",
                portfolio_detail="last-known values are retained for diagnosis only",
                errors=self.snapshot.errors
                + (
                    {
                        "condition": "broker_unavailable",
                        "message": "broker refresh failed",
                        "created_at": now.isoformat(),
                    },
                ),
                updated_at=now,
            )
            return self.snapshot

        self.snapshot = replace(
            self.snapshot,
            connectivity_status="healthy",
            connectivity_detail="broker state refreshed",
            connectivity_checked_at=now,
            portfolio_status="current",
            portfolio_detail="broker-authoritative snapshot",
            balances=balances,
            positions=positions,
            updated_at=now,
        )
        return self.snapshot

    async def emit_alert(self, alert: Alert) -> tuple[AlertDelivery, ...]:
        deliveries = await self.alert_router.route(alert)
        delivery_payload = [
            {"destination": item.destination, "status": item.status} for item in deliveries
        ]
        self.snapshot = replace(
            self.snapshot,
            alerts=self.snapshot.alerts
            + (
                {
                    "condition": alert.condition,
                    "severity": alert.severity,
                    "message": alert.message,
                    "created_at": alert.created_at.isoformat(),
                    "deliveries": delivery_payload,
                },
            ),
            updated_at=utc_now(),
        )
        return deliveries

    def dismiss_alerts(self, *, through: int, actor: str, journal: SystemEventJournal) -> int:
        """Mark the first ``through`` alerts read; return how many were newly dismissed.

        ``through`` is the alert count the administrator's page showed, so an alert that arrived
        after that page rendered stays new. Alerts are kept, never removed. The event is saved
        first: if the journal raises, nothing is dismissed. No ``await`` sits between reading
        the list and replacing it, so a concurrent ``emit_alert`` cannot be lost.
        """

        alerts = self.snapshot.alerts
        covered = alerts[: max(through, 0)]
        fresh = [alert for alert in covered if "dismissed_at" not in alert]
        if not fresh:
            return 0
        now = utc_now()
        journal.record(
            ALERTS_DISMISSED_EVENT,
            {
                "actor": actor,
                "count": len(fresh),
                "newest_alert_at": max(str(alert.get("created_at", "")) for alert in fresh),
            },
            now=now,
        )
        marked = tuple(
            {**alert, "dismissed_at": now.isoformat(), "dismissed_by": actor}
            if index < len(covered) and "dismissed_at" not in alert
            else alert
            for index, alert in enumerate(alerts)
        )
        self.snapshot = replace(self.snapshot, alerts=marked, updated_at=now)
        return len(fresh)

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if self.snapshot.connectivity_status == "healthy" else "degraded",
            "application": {
                "status": "healthy",
                "version": application_version(),
                "heartbeat": utc_now().isoformat(),
            },
            "broker": {
                "status": self.snapshot.connectivity_status,
                "detail": self.snapshot.connectivity_detail,
                "checked_at": _iso(self.snapshot.connectivity_checked_at),
            },
            "strategies": [
                {
                    "name": item.name,
                    "version": item.version,
                    "status": item.status,
                    "last_seen": _iso(item.last_seen),
                    "detail": item.detail,
                }
                for item in self.snapshot.strategy_heartbeats
            ],
        }

    def transitions(self, limit: int = TRANSITION_LIMIT) -> list[dict[str, Any]]:
        """The newest kill-switch transitions, newest first, including persisted ones."""

        fields = ("from", "to", "actor", "automatic", "reason", "created_at")
        return [
            {key: event.get(key) for key in fields}
            for event in reversed(self.kill_switch.audit_events[-limit:])
        ]

    def to_dict(self) -> dict[str, Any]:
        snapshot = self.snapshot
        health = self.health()
        return {
            "application": {
                "status": health["status"],
                "version": health["application"]["version"],
                "heartbeat": health["application"]["heartbeat"],
            },
            "trading": {
                "mode": self.settings.trading_mode.value,
                "credential_scope": self.settings.credential_scope.value,
                "strategy_version": snapshot.strategy_version,
            },
            "connectivity": {
                "status": snapshot.connectivity_status,
                "detail": snapshot.connectivity_detail,
                "checked_at": _iso(snapshot.connectivity_checked_at),
            },
            "risk": {
                "kill_switch": self.kill_switch.state.value,
                "transitions": self.transitions(),
            },
            "recovery": (
                self.startup_recovery.to_dict()
                if self.startup_recovery is not None
                else {"status": "not_run", "detail": "startup recovery has not run"}
            ),
            "reconciliation": (
                self.scheduled_reconciliation.status.to_dict()
                if self.scheduled_reconciliation is not None
                else {"last_result": "not_scheduled"}
            ),
            "runtime": {
                "status": snapshot.runtime_status,
                "detail": snapshot.runtime_detail,
                "last_cycle_status": snapshot.runtime_last_cycle_status,
                "last_cycle_at": _iso(snapshot.runtime_last_cycle_at),
            },
            # Kept apart from runtime and the heartbeats: a watch-only symbol's trouble
            # is never the trading feed's.
            "watch_feed": (
                self.watch_feed.to_dict()
                if self.watch_feed is not None
                else {"enabled": False, "symbols": []}
            ),
            "portfolio": {
                "status": snapshot.portfolio_status,
                "detail": snapshot.portfolio_detail,
                "balances": [_model_payload(item) for item in snapshot.balances],
                "positions": [_model_payload(item) for item in snapshot.positions],
                "pnl": {"status": snapshot.pnl_status, "detail": snapshot.pnl_detail},
            },
            "orders": [_model_payload(item) for item in snapshot.orders],
            "fills": [_model_payload(item) for item in snapshot.fills],
            "signals": [_model_payload(item) for item in snapshot.signals],
            "strategies": health["strategies"],
            "alerts": list(snapshot.alerts),
            "errors": list(snapshot.errors),
            "alert_destinations": list(self.alert_router.configured_destinations),
            "updated_at": snapshot.updated_at.isoformat(),
        }


def _model_payload(value: object) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {"value": str(value)}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
