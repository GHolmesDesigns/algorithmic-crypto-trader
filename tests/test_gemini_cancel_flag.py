"""#168: an order Gemini reports as fully executed is filled even if it is also flagged cancelled.

On 2026-10-17 a fully filled own buy came back from order/status with a cancelled flag, the
adapter cached it as CANCELED, and the scheduled reconciliation halted trading over a
filled-versus-canceled divergence with matching quantity, fills and balances.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest
from brokers.gemini import GeminiBroker
from core.models import OrderStatus, OrderType
from core.resilience import TokenBucketRateLimiter
from portfolio.reconciliation import PortfolioState, Reconciler
from risk.kill_switch import KillSwitch

from tests.test_phase1_gate_gemini_lifecycle import GeminiSandboxBook, gemini, request

FLAG_LOGGER = "brokers.gemini"


async def filled_then_flagged(
    book: GeminiSandboxBook, *, quantity: str = "0.01", reason: str = "Requested"
) -> tuple[GeminiBroker, str]:
    """An IOC buy that trades in full; the status endpoint later reports it cancelled."""

    broker = gemini(book)
    order_request, approval = request(quantity=quantity)
    acknowledged = await broker.submit_order(order_request, approval)
    assert acknowledged.status is OrderStatus.FILLED
    client_id = str(order_request.client_order_id)
    order = book.orders[book.by_client[client_id]]
    order["is_cancelled"] = True
    order["reason_override"] = reason
    return broker, client_id


@pytest.fixture(autouse=True)
def reason_override(monkeypatch: pytest.MonkeyPatch) -> None:
    original = GeminiSandboxBook.payload

    def payload(self, order, *, include_trades):  # type: ignore[no-untyped-def]
        body = original(self, order, include_trades=include_trades)
        if "reason_override" in order:
            body["reason"] = order["reason_override"]
        return body

    monkeypatch.setattr(GeminiSandboxBook, "payload", payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Requested", "Cancelled"])
async def test_a_fully_executed_order_flagged_cancelled_stays_filled(reason: str) -> None:
    book = GeminiSandboxBook()
    broker, client_id = await filled_then_flagged(book, reason=reason)

    fills = await broker.get_fills(client_id)  # re-reads order/status, as the reconciler does
    order = await broker.get_order(client_id)

    assert order is not None
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal("0.01")
    assert sum(fill.quantity for fill in fills) == Decimal("0.01")
    await broker.close()


@pytest.mark.asyncio
async def test_a_fresh_adapter_reading_the_flagged_order_reports_filled() -> None:
    book = GeminiSandboxBook()
    broker, client_id = await filled_then_flagged(book)
    restarted = gemini(book)  # no cache: the status reply alone decides

    order = await restarted.get_order(client_id)

    assert order is not None and order.status is OrderStatus.FILLED
    await broker.close()
    await restarted.close()


class StatusOnlyGemini(GeminiBroker):
    """The real order and fill reads; balances and positions are not under test."""

    async def get_balances(self):  # type: ignore[no-untyped-def]
        return ()

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return ()


@pytest.mark.asyncio
async def test_reconciliation_does_not_halt_on_a_filled_order_flagged_cancelled() -> None:
    book = GeminiSandboxBook()
    submitter = gemini(book)
    order_request, approval = request()
    client_id = str(order_request.client_order_id)
    local_order = await submitter.submit_order(order_request, approval)
    local_fills = {fill.fill_id: fill for fill in await submitter.get_fills(client_id)}
    book.orders[book.by_client[client_id]]["is_cancelled"] = True
    broker = StatusOnlyGemini(
        api_key="sandbox-key",
        api_secret="sandbox-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(book.handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
    )
    switch = KillSwitch()

    result = await Reconciler(broker, switch).reconcile(
        PortfolioState(orders={client_id: local_order}, fills=local_fills)
    )

    assert result.discrepancies == ()
    assert result.safety_tripped is False
    assert switch.state.value == "running"
    await submitter.close()
    await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("executed", ["0", "0.004"])
async def test_a_short_or_empty_execution_flagged_cancelled_stays_canceled(executed: str) -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    order_request, approval = request(order_type=OrderType.LIMIT, limit_price="50000")
    await broker.submit_order(order_request, approval)
    client_id = str(order_request.client_order_id)
    venue_id = book.by_client[client_id]
    if executed != "0":
        book.fill(venue_id, executed, "50000")
    book.orders[venue_id]["is_live"] = False
    book.orders[venue_id]["is_cancelled"] = True

    restarted = gemini(book)
    order = await restarted.get_order(client_id)

    assert order is not None
    assert order.status is OrderStatus.CANCELED
    assert order.filled_quantity == Decimal(executed)
    await broker.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_a_flagged_order_with_executions_logs_only_the_allow_listed_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    book = GeminiSandboxBook()
    broker, client_id = await filled_then_flagged(book)

    with caplog.at_level(logging.WARNING, logger=FLAG_LOGGER):
        await broker.get_fills(client_id)

    lines = [record.getMessage() for record in caplog.records if record.name == FLAG_LOGGER]
    assert len(lines) == 1
    line = lines[0]
    for expected in (
        "is_cancelled=True",
        "reason=Requested",
        "is_live=False",
        "executed_amount=0.01",
        "original_amount=0.01",
        "remaining_amount=0",
        client_id[:8],
    ):
        assert expected in line
    provider_id = str(book.by_client[client_id])
    for forbidden in (client_id, provider_id, "sandbox-key", "sandbox-secret", "60010"):
        assert forbidden not in line
    await broker.close()


@pytest.mark.asyncio
async def test_a_plain_fill_or_a_plain_cancel_logs_no_flag_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    order_request, approval = request()
    with caplog.at_level(logging.WARNING, logger=FLAG_LOGGER):
        await broker.submit_order(order_request, approval)
        await broker.get_fills(str(order_request.client_order_id))
        resting, resting_approval = request(order_type=OrderType.LIMIT, limit_price="50000")
        await broker.submit_order(resting, resting_approval)
        await broker.cancel_order(str(resting.client_order_id))

    assert [r for r in caplog.records if r.name == FLAG_LOGGER] == []
    await broker.close()


@pytest.mark.asyncio
async def test_cancel_and_replace_of_an_order_that_filled_first_returns_it_filled() -> None:
    book = GeminiSandboxBook()
    broker = gemini(book)
    done, approval = request(order_type=OrderType.LIMIT, limit_price="50000")
    await broker.submit_order(done, approval)
    book.fill(book.by_client[str(done.client_order_id)], "0.01", "50000")

    kept = await broker.edit_order(
        str(done.client_order_id),
        quantity=Decimal("0.01"),
        limit_price=Decimal("49000"),
        approval=approval,
    )

    assert kept.status is OrderStatus.FILLED
    assert kept.filled_quantity == Decimal("0.01")
    assert len(book.orders) == 1  # no replacement was sent
    await broker.close()
