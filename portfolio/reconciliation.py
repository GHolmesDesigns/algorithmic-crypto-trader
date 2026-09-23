"""Broker-authoritative portfolio reconciliation and safety response."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from brokers.interface import BrokerInterface
from core.models import Balance, Fill, Order, Position
from risk.kill_switch import KillSwitch


@dataclass(frozen=True)
class PortfolioState:
    orders: dict[str, Order] = field(default_factory=dict)
    fills: dict[str, Fill] = field(default_factory=dict)
    positions: tuple[Position, ...] = ()
    balances: tuple[Balance, ...] = ()


@dataclass(frozen=True)
class Discrepancy:
    entity_type: str
    entity_key: str
    local: object
    broker: object


@dataclass(frozen=True)
class ReconciliationResult:
    authoritative: PortfolioState
    discrepancies: tuple[Discrepancy, ...]
    safety_tripped: bool


class ReconciliationUnavailable(RuntimeError):
    pass


class Reconciler:
    def __init__(
        self,
        broker: BrokerInterface,
        kill_switch: KillSwitch,
        alert: Callable[[Discrepancy], None] | None = None,
        store=None,
    ) -> None:
        self.broker = broker
        self.kill_switch = kill_switch
        self.alert = alert
        self.store = store

    async def reconcile(self, local: PortfolioState) -> ReconciliationResult:
        try:
            positions = await self.broker.get_positions()
            balances = await self.broker.get_balances()
        except Exception as exc:
            self.kill_switch.trip("broker unavailable during reconciliation")
            raise ReconciliationUnavailable("broker state could not be read") from exc

        broker_orders: dict[str, Order] = {}
        broker_fills: dict[str, Fill] = {}
        discrepancies: list[Discrepancy] = []
        for client_order_id, local_order in local.orders.items():
            try:
                broker_order = await self.broker.get_order(client_order_id)
            except Exception as exc:
                self.kill_switch.trip("broker unavailable during order reconciliation")
                raise ReconciliationUnavailable("broker order state could not be read") from exc
            if broker_order is None:
                discrepancies.append(
                    Discrepancy("order", client_order_id, local_order.status.value, None)
                )
                continue
            broker_orders[client_order_id] = broker_order
            if (
                broker_order.status != local_order.status
                or broker_order.filled_quantity != local_order.filled_quantity
            ):
                discrepancies.append(
                    Discrepancy(
                        "order",
                        client_order_id,
                        (local_order.status.value, str(local_order.filled_quantity)),
                        (broker_order.status.value, str(broker_order.filled_quantity)),
                    )
                )
            try:
                remote_fills = await self.broker.get_fills(client_order_id)
            except Exception as exc:
                self.kill_switch.trip("broker unavailable during fill reconciliation")
                raise ReconciliationUnavailable("broker fill state could not be read") from exc
            for fill in remote_fills:
                broker_fills[fill.fill_id] = fill

        local_positions = {item.symbol: item for item in local.positions}
        remote_positions = {item.symbol: item for item in positions}
        for symbol in sorted(set(local_positions) | set(remote_positions)):
            local_position = local_positions.get(symbol)
            remote_position = remote_positions.get(symbol)
            if (
                local_position is None
                or remote_position is None
                or local_position.quantity != remote_position.quantity
                or local_position.average_price != remote_position.average_price
            ):
                discrepancies.append(
                    Discrepancy(
                        "position",
                        symbol,
                        local_positions.get(symbol),
                        remote_positions.get(symbol),
                    )
                )

        local_balances = {item.asset: item for item in local.balances}
        remote_balances = {item.asset: item for item in balances}
        for asset in sorted(set(local_balances) | set(remote_balances)):
            local_balance = local_balances.get(asset)
            remote_balance = remote_balances.get(asset)
            if (
                local_balance is None
                or remote_balance is None
                or local_balance.available != remote_balance.available
                or local_balance.hold != remote_balance.hold
            ):
                discrepancies.append(
                    Discrepancy(
                        "balance", asset, local_balances.get(asset), remote_balances.get(asset)
                    )
                )

        for fill_id in set(local.fills) - set(broker_fills):
            discrepancies.append(Discrepancy("fill", fill_id, local.fills[fill_id], None))
        for fill_id in set(broker_fills) - set(local.fills):
            discrepancies.append(Discrepancy("fill", fill_id, None, broker_fills[fill_id]))

        for discrepancy in discrepancies:
            if self.alert is not None:
                self.alert(discrepancy)
        if discrepancies:
            self.kill_switch.trip("broker-authoritative reconciliation divergence")

        authoritative = PortfolioState(
            orders=broker_orders,
            fills=broker_fills,
            positions=positions,
            balances=balances,
        )
        if self.store is not None:
            try:
                self.store.save_snapshot(authoritative, source="broker")
                for discrepancy in discrepancies:
                    self.store.save_discrepancy(
                        discrepancy,
                        safety_action="halted" if discrepancies else "none",
                    )
            except Exception as exc:
                self.kill_switch.trip("portfolio persistence unavailable during reconciliation")
                raise ReconciliationUnavailable(
                    "authoritative portfolio state could not be persisted"
                ) from exc

        return ReconciliationResult(
            authoritative=authoritative,
            discrepancies=tuple(discrepancies),
            safety_tripped=bool(discrepancies),
        )
