"""Immutable, Decimal-based domain models shared by every package."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveDecimal = Annotated[Decimal, Field(gt=Decimal("0"))]
NonNegativeDecimal = Annotated[Decimal, Field(ge=Decimal("0"))]


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    REPLAY = "replay"
    PAPER = "paper"
    LIVE = "live"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"
    UNKNOWN = "unknown"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Candle(FrozenModel):
    symbol: str
    interval: str
    opened_at: datetime
    closed_at: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal
    source: str
    as_of: datetime
    ingested_at: datetime = Field(default_factory=utc_now)

    @field_validator("closed_at")
    @classmethod
    def close_after_open(cls, value: datetime, info: object) -> datetime:
        opened_at = getattr(info, "data", {}).get("opened_at")
        if opened_at is not None and value <= opened_at:
            raise ValueError("closed_at must be after opened_at")
        return value

    @field_validator("high")
    @classmethod
    def high_is_high(cls, value: Decimal, info: object) -> Decimal:
        data = getattr(info, "data", {})
        if "open" in data and value < data["open"]:
            raise ValueError("high must be at least open")
        return value

    @field_validator("low")
    @classmethod
    def low_is_low(cls, value: Decimal, info: object) -> Decimal:
        data = getattr(info, "data", {})
        if "high" in data and value > data["high"]:
            raise ValueError("low must not exceed high")
        return value

    @field_validator("close")
    @classmethod
    def close_is_within_bar(cls, value: Decimal, info: object) -> Decimal:
        data = getattr(info, "data", {})
        if "high" in data and value > data["high"]:
            raise ValueError("close must not exceed high")
        if "low" in data and value < data["low"]:
            raise ValueError("close must not be below low")
        return value


class Quote(FrozenModel):
    symbol: str
    bid: PositiveDecimal
    ask: PositiveDecimal
    as_of: datetime
    source: str
    received_at: datetime = Field(default_factory=utc_now)

    @field_validator("ask")
    @classmethod
    def ask_not_below_bid(cls, value: Decimal, info: object) -> Decimal:
        bid = getattr(info, "data", {}).get("bid")
        if bid is not None and value < bid:
            raise ValueError("ask must not be below bid")
        return value


class Balance(FrozenModel):
    asset: str
    available: NonNegativeDecimal
    hold: NonNegativeDecimal = Decimal("0")
    as_of: datetime


class Position(FrozenModel):
    symbol: str
    quantity: Decimal
    average_price: NonNegativeDecimal
    as_of: datetime


class Signal(FrozenModel):
    signal_id: UUID = Field(default_factory=uuid4)
    symbol: str
    side: OrderSide
    quantity: PositiveDecimal
    strategy_version: str
    created_at: datetime = Field(default_factory=utc_now)
    correlation_id: UUID = Field(default_factory=uuid4)


class OrderRequest(FrozenModel):
    signal_id: UUID
    strategy_version: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: PositiveDecimal
    limit_price: NonNegativeDecimal | None = None
    client_order_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID

    @model_validator(mode="after")
    def validate_price(self) -> OrderRequest:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders must not set limit_price")
        return self


class Order(FrozenModel):
    order_id: UUID = Field(default_factory=uuid4)
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    filled_quantity: NonNegativeDecimal = Decimal("0")
    average_fill_price: NonNegativeDecimal | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Fill(FrozenModel):
    fill_id: str
    order_id: UUID
    symbol: str
    side: OrderSide
    quantity: PositiveDecimal
    price: PositiveDecimal
    fee: NonNegativeDecimal
    fee_asset: str
    occurred_at: datetime


class MarketState(FrozenModel):
    symbol: str
    quote: Quote
    candles: tuple[Candle, ...] = ()
    observed_at: datetime = Field(default_factory=utc_now)


class RiskApproval(FrozenModel):
    approval_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    approved: bool
    reason: str
    approved_at: datetime = Field(default_factory=utc_now)
    correlation_id: UUID
