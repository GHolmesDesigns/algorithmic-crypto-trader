"""What a reconciliation divergence differed in, as one log line per differing field.

A ``Discrepancy`` says which entity disagreed. This says which field, by how much, and, where it
is safe to, what each side held: an operator or an agent reading the log should not have to open
the database to learn that USD ``available`` was 0.000002 apart.

Values follow the trading mode. ``paper`` runs against a sandbox, so a line carries both values
in full. Anywhere else a line carries the field name and the delta only, because a balance the
venue reports for a real account must never reach a log. The delta is the broker's value minus
the local one: the broker is authoritative, so a positive delta means the broker holds more.

Nothing here reads the broker or the store, writes a row, or changes what reconciliation
compares or when it halts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from core.logging import fill_reference, short_reference

if TYPE_CHECKING:
    from portfolio.reconciliation import Discrepancy

PRESENCE = "presence"
_ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    local: str | None
    broker: str | None
    # Broker minus local. ``None`` when the field is not a number or one side is absent.
    delta: Decimal | None


def field_changes(discrepancy: Discrepancy) -> tuple[FieldChange, ...]:
    """The fields that differ, or one ``presence`` change when only one side has the entity."""

    kind = discrepancy.entity_type
    # A discrepancy holds whatever object reconciliation compared; ``kind`` says which.
    local: Any = discrepancy.local
    broker: Any = discrepancy.broker
    if local is None or broker is None:
        return (FieldChange(PRESENCE, _render(kind, local), _render(kind, broker), None),)

    changes: list[FieldChange] = []
    if kind == "order" and isinstance(local, tuple) and isinstance(broker, tuple):
        # The reconciler records an order's two compared fields as (status, filled quantity) text.
        changes += _compared("status", local[0], broker[0])
        changes += _compared("filled_quantity", local[1], broker[1])
    elif kind == "position":
        changes += _compared("quantity", local.quantity, broker.quantity)
        changes += _compared("average_price", local.average_price, broker.average_price)
    elif kind == "balance":
        changes += _compared("available", local.available, broker.available)
        changes += _compared("hold", local.hold, broker.hold)
    if not changes:
        # Both sides present but no field this module knows differs: show the values as given.
        changes.append(FieldChange("value", _render(kind, local), _render(kind, broker), None))
    return tuple(changes)


def log_discrepancy(
    logger: logging.Logger, discrepancy: Discrepancy, *, include_values: bool
) -> None:
    """Log each differing field of ``discrepancy`` once, at error level.

    ``include_values`` is the caller's statement that both values may be logged in full. It is
    ``False`` unless the caller says otherwise, so a new caller cannot leak a live balance by
    forgetting a flag.
    """

    kind = discrepancy.entity_type
    key = _short(kind, discrepancy.entity_key)
    for change in field_changes(discrepancy):
        delta = "n/a" if change.delta is None else format(change.delta, "+f")
        event: dict[str, object] = {
            "kind": kind,
            "key": key,
            "field": change.field,
            "delta": delta,
        }
        message = "reconciliation divergence: %s %s field=%s delta(broker-local)=%s"
        args: tuple[object, ...] = (kind, key, change.field, delta)
        if include_values:
            event["local"], event["broker"] = change.local, change.broker
            message += " local=%s broker=%s"
            args += (change.local, change.broker)
        logger.error(message, *args, extra={"event": event})


def _compared(field: str, local: object, broker: object) -> list[FieldChange]:
    if local is None or broker is None or local == broker:
        return []
    return [FieldChange(field, str(local), str(broker), _delta(local, broker))]


def _delta(local: object, broker: object) -> Decimal | None:
    try:
        return Decimal(str(broker)) - Decimal(str(local))
    except InvalidOperation:
        return None


def _short(kind: str, key: str) -> str:
    # An order or fill key is an identifier; an asset or symbol is not.
    if kind == "order":
        return short_reference(key)
    return fill_reference(key) if kind == "fill" else key


def _render(kind: str, value: object) -> str:
    if value is None:
        return _ABSENT
    if isinstance(value, str):
        return value
    names = {
        "balance": ("available", "hold"),
        "position": ("quantity", "average_price"),
        "fill": ("quantity", "price", "fee"),
    }.get(kind)
    if names is None:
        return str(value)
    return " ".join(f"{name}={getattr(value, name, None)}" for name in names)
