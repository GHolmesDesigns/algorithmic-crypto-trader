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
from pydantic import Field


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
