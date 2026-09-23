from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from core.models import Candle, OrderRequest, OrderSide, OrderType, Quote
from pydantic import ValidationError

NOW = datetime.now(UTC)


def test_candle_is_frozen_and_uses_decimal_money() -> None:
    candle = Candle(
        symbol="BTC-USD",
        interval="1m",
        opened_at=NOW,
        closed_at=NOW + timedelta(seconds=1),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1.25"),
        source="fixture",
        as_of=NOW,
    )
    assert candle.close == Decimal("105")
    with pytest.raises(ValidationError):
        candle.close = Decimal("106")  # type: ignore[misc]


def test_quote_rejects_crossed_market() -> None:
    with pytest.raises(ValidationError, match="ask"):
        Quote(symbol="BTC-USD", bid=Decimal("101"), ask=Decimal("100"), as_of=NOW, source="fixture")


def test_order_request_requires_explicit_limit_price() -> None:
    common = dict(
        signal_id=uuid4(),
        strategy_version="test-v1",
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        correlation_id=uuid4(),
    )
    with pytest.raises(ValidationError, match="limit_price"):
        OrderRequest(order_type=OrderType.LIMIT, **common)
    request = OrderRequest(order_type=OrderType.MARKET, **common)
    assert request.limit_price is None
