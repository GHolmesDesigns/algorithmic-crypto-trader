"""The provider-neutral broker contract used by strategy-independent code."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Literal

from core.models import (
    Balance,
    Fill,
    FrozenModel,
    Order,
    OrderRequest,
    Position,
    Quote,
    RiskApproval,
)
from pydantic import Field, field_validator


class BrokerCapabilities(FrozenModel):
    provider: str
    environment: Literal["local", "sandbox", "production"]
    streaming: bool
    historical_candles: bool
    native_order_edit: bool
    preview_orders: bool
    order_types: tuple[str, ...] = Field(min_length=1)
    price_increment: Decimal
    quantity_increment: Decimal
    max_quote_age_seconds: int = Field(gt=0)
    # The unit a venue settles an asset's balance in, e.g. {"USD": Decimal("0.00001")} for a
    # venue that keeps dollars to 5 decimals. A fill's notional can carry more decimals than
    # that, so the ledger cuts every amount a fill moves in a listed asset toward zero to the
    # same unit, fill by fill, before the exact comparison. An asset not listed is compared at
    # full precision (#118).
    balance_increments: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("balance_increments")
    @classmethod
    def increments_are_positive(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        for asset, increment in value.items():
            if increment <= 0:
                raise ValueError(f"balance increment for {asset} must be positive")
        return value


class BrokerInterface(ABC):
    """All broker implementations expose the same async surface."""

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    async def get_balances(self) -> tuple[Balance, ...]:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> tuple[Position, ...]:
        raise NotImplementedError

    @abstractmethod
    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        """Submit only an already-approved request; raw signals are not accepted."""
        raise NotImplementedError

    @abstractmethod
    async def get_order(self, client_order_id: str) -> Order | None:
        raise NotImplementedError

    @abstractmethod
    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        raise NotImplementedError
