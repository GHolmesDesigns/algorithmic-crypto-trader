"""Provider-neutral state assembly for the authenticated operator surface."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from brokers.interface import BrokerInterface
from core.guards import StartupSettings
from core.models import Balance, Fill, Order, Position, Signal, utc_now
from risk.kill_switch import KillSwitch

from api.alerts import Alert, AlertDelivery, AlertRouter


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
                heartbeats.append(
                    replace(item, status=status, last_seen=utc_now(), detail=detail)
                )
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
        except Exception:
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
            {"destination": item.destination, "status": item.status}
            for item in deliveries
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

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy"
            if self.snapshot.connectivity_status == "healthy"
            else "degraded",
            "application": {"status": "healthy", "heartbeat": utc_now().isoformat()},
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

    def to_dict(self) -> dict[str, Any]:
        snapshot = self.snapshot
        health = self.health()
        return {
            "application": {
                "status": health["status"],
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
            "risk": {"kill_switch": self.kill_switch.state.value},
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
