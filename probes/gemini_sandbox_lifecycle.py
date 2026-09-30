"""Owner-run: the Gemini Sandbox order lifecycle and one trading-loop pass.

Everything goes through ``GeminiBroker``, which refuses any host outside
``*.sandbox.gemini.com``, so no production account can be reached. Orders are
tiny, the request budget is fixed, and any order still resting at the end is
canceled in ``finally``. The Sandbox key must be created for the Primary account
(a group-level Master key is refused) with the Trader role only, and the account
needs test US dollars, which the first step checks before any order.

Run from the repository root and type the key and secret into the hidden prompts::

    python -m probes.gemini_sandbox_lifecycle

The redacted result records statuses, rejection reasons, and counts: no key,
order identifier, or balance. It names any holding the trading-loop step left out
because the Sandbox could not price it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any
from uuid import uuid4

import httpx
from app.trading import BrokerRiskInputs, CycleStatus, TradingCycle
from brokers.gemini import GEMINI_SANDBOX_REST_URL, GeminiBroker
from brokers.http import ProviderOrderRejectedError
from brokers.interface import BrokerCapabilities, BrokerInterface
from core.models import (
    Balance,
    Candle,
    Fill,
    MarketState,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RiskApproval,
)
from execution.audit import InMemoryAuditStore, unlinked_orders
from execution.engine import ExecutionEngine, InMemoryOrderStore
from risk.engine import ExchangeConstraints, RiskLimits
from risk.kill_switch import KillSwitch
from strategy.reference import AlwaysBuyStrategy

from probes.common import (
    BoundedTransport,
    BudgetExhausted,
    ProbeReport,
    UnexpectedHost,
    bounded_client,
    prompt_secret,
    run_probe,
)

SANDBOX_HOST = "api.sandbox.gemini.com"
SYMBOL = "BTC-USD"
# About 35 requests, plus two quotes per Sandbox currency when the loop values holdings.
REQUEST_BUDGET = 80
CLEANUP_RESERVE = 10
PROBE_SIZE = Decimal("0.0001")  # BTC; well above the documented 0.00001 minimum
UNDERSIZED = Decimal("0.000001")
PARTIAL_FILL_CAP = Decimal("0.01")
# The Sandbox's best ask changes from moment to moment, so read it a few times.
BOOK_READS = 5
BOOK_PAUSE_SECONDS = 2.0
CENT = Decimal("0.01")
BAR = timedelta(minutes=5)
CONSTRAINTS = ExchangeConstraints(
    min_quantity=Decimal("0.00001"),
    quantity_increment=Decimal("0.00000001"),
    min_notional=CENT,
    price_increment=CENT,
)
# Sandbox balances are play money, so the exposure limits only need to admit one tiny
# order; the price, staleness, volatility, and slippage gates keep their defaults.
PROBE_LIMITS = RiskLimits(
    max_open_positions=1_000,
    max_trade_notional=Decimal("100"),
    max_symbol_position=Decimal("1000000"),
    max_aggregate_allocation=Decimal("1000000000000"),
    min_cash_reserve=Decimal("0"),
    max_daily_loss=Decimal("1000000000000"),
    max_drawdown=Decimal("1"),
)
LIVE = frozenset(
    {
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.UNKNOWN,
    }
)
PASSING = frozenset({"pass", "answered"})


class SandboxRun:
    """The probe's adapter; ``restart`` replaces it, as a process restart would."""

    def __init__(self, api_key: str, api_secret: str, client: httpx.AsyncClient) -> None:
        self._credentials = (api_key, api_secret)
        self.client = client
        self.broker = self._adapter()
        self.resting: list[str] = []

    def _adapter(self) -> GeminiBroker:
        api_key, api_secret = self._credentials
        return GeminiBroker(api_key=api_key, api_secret=api_secret, client=self.client)

    def restart(self) -> GeminiBroker:
        # Gemini nonces come from the clock, so the new adapter's nonces keep increasing;
        # the old adapter is never used again.
        self.broker = self._adapter()
        return self.broker

    async def submit(
        self, order_type: OrderType, quantity: Decimal, limit_price: Decimal | None = None
    ) -> tuple[str, Order]:
        request = OrderRequest(
            signal_id=uuid4(),
            strategy_version="owner-run-gemini-probe",
            symbol=SYMBOL,
            side=OrderSide.BUY,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            correlation_id=uuid4(),
        )
        # The lifecycle steps exercise the adapter, not the risk engine, which the
        # trading-loop step runs in full.
        approval = RiskApproval(
            signal_id=request.signal_id,
            approved=True,
            reason="owner-run Gemini Sandbox lifecycle probe",
            correlation_id=request.correlation_id,
        )
        key = str(request.client_order_id)
        order = await self.broker.submit_order(request, approval)
        if order.status in LIVE:
            self.resting.append(key)
        return key, order


class PricedHoldingsView(BrokerInterface):
    """The adapter as this check's trading loop sees it, minus holdings it cannot price.

    Sandbox accounts come with coins whose test books can have no buyers, and the
    trading loop rightly refuses to buy while any holding cannot be priced. Only this
    owner-run check leaves those positions out, and it names them in its result; the
    trading bot's own rule is unchanged. Everything else passes straight through.
    """

    def __init__(self, broker: GeminiBroker, left_out: frozenset[str]) -> None:
        self.broker = broker
        self.left_out = left_out

    @property
    def healthy(self) -> bool:
        return self.broker.healthy

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self.broker.capabilities

    async def get_quote(self, symbol: str) -> Quote:
        return await self.broker.get_quote(symbol)

    async def get_balances(self) -> tuple[Balance, ...]:
        return await self.broker.get_balances()

    async def get_positions(self) -> tuple[Position, ...]:
        positions = await self.broker.get_positions()
        return tuple(item for item in positions if item.symbol not in self.left_out)

    async def submit_order(self, request: OrderRequest, approval: RiskApproval) -> Order:
        return await self.broker.submit_order(request, approval)

    async def get_order(self, client_order_id: str) -> Order | None:
        return await self.broker.get_order(client_order_id)

    async def get_fills(self, client_order_id: str) -> tuple[Fill, ...]:
        return await self.broker.get_fills(client_order_id)


async def lifecycle(
    api_key: str,
    api_secret: str,
    *,
    inner: httpx.AsyncBaseTransport | None = None,
) -> ProbeReport:
    report = ProbeReport(probe="gemini-sandbox-lifecycle", target=GEMINI_SANDBOX_REST_URL)
    report.keep_out(api_key, api_secret)
    client, transport = bounded_client(
        hosts={SANDBOX_HOST}, budget=REQUEST_BUDGET, reserve=CLEANUP_RESERVE, inner=inner
    )
    run = SandboxRun(api_key, api_secret, client)
    try:
        quote = await _funded_quote(run, report)
        if quote is not None:
            await _capped_market_order(run, report)
            await _resting_limit_and_recovery(run, report, quote)
            await _undersized(run, report, quote)
            await _partial_fill(run, report)
            await _trading_loop(run, report)
    except Exception as exc:
        # Anything, even a bug, is recorded as a failed step: the result is still written
        # and cleanup still runs. Only the error's type and Gemini's reason code are kept.
        report.step("stopped", "fail", **_describe(exc))
    finally:
        await _cancel_leftovers(run, report, transport)
        await client.aclose()
        report.count_requests(transport)
    report.notes.append(
        "A lost response (timeout) is not induced; recovery by client_order_id is checked "
        "through a restarted adapter instead."
    )
    report.outcome = (
        "pass" if all(step["result"] in PASSING for step in report.steps) else "needs review"
    )
    return report


async def _funded_quote(run: SandboxRun, report: ProbeReport) -> Quote | None:
    """Authenticate, then confirm the account holds enough US dollars for every order.

    Returns the quote, or ``None`` when the run should stop before placing anything.
    Only whether the balance suffices is recorded, never the balance itself.
    """

    balances = await run.broker.get_balances()
    report.step("authenticate", "pass", currencies=len(balances))
    quote = await run.broker.get_quote(SYMBOL)
    usd = next((item.available for item in balances if item.asset == "USD"), Decimal("0"))
    # Two capped buys, one resting buy, and a partial fill of up to the cap, plus fees.
    needed = _price(quote.ask * (3 * PROBE_SIZE + PARTIAL_FILL_CAP) * Decimal("1.03")) + CENT
    enough = usd >= needed
    report.step("usd_available", _check(enough), needed_usd=str(needed))
    if not enough:
        report.notes.append(
            f"The Sandbox Primary account needs at least {needed} USD for this check. The "
            "Sandbox has no add-funds function (Gemini's documentation, read 2026-09-30): ask "
            "Gemini to adjust the balance at trading@gemini.com, or use a new Sandbox account, "
            "which is credited automatically and needs a new API key. Then run it again."
        )
        return None
    return quote


async def _capped_market_order(run: SandboxRun, report: ProbeReport) -> None:
    """A market request goes out as an immediate-or-cancel limit capped 1% past the ask."""

    try:
        key, order = await run.submit(OrderType.MARKET, PROBE_SIZE)
    except ProviderOrderRejectedError as exc:
        report.step("capped_market_order", "fail", reason=str(exc))
        return
    fills = await run.broker.get_fills(key)
    filled = order.status is OrderStatus.FILLED
    report.step(
        "capped_market_order",
        "pass" if filled else "refused",
        status=order.status.value,
        filled=str(order.filled_quantity),
        fills=len(fills),
    )
    if not filled:
        report.notes.append(
            "The capped order did not fill completely within 1% of the Sandbox quote, so "
            "the Sandbox book is thin near its quote; the unfilled part was cancelled."
        )


async def _resting_limit_and_recovery(run: SandboxRun, report: ProbeReport, quote: Quote) -> None:
    price = _price(quote.bid / 2)  # far below the market, so the order rests
    key, order = await run.submit(OrderType.LIMIT, PROBE_SIZE, price)
    report.step(
        "resting_limit", _check(order.status is OrderStatus.OPEN), status=order.status.value
    )
    restarted = run.restart()
    recovered = await restarted.get_order(key)
    report.step(
        "recovery_by_client_order_id",
        _check(recovered is not None and recovered.status is OrderStatus.OPEN),
        status=recovered.status.value if recovered else "not found",
    )
    await restarted.cancel_order(key)
    confirmed = await run.restart().get_order(key)
    canceled = confirmed is not None and confirmed.status is OrderStatus.CANCELED
    report.step(
        "cancel", _check(canceled), status=confirmed.status.value if confirmed else "not found"
    )
    if canceled and key in run.resting:
        run.resting.remove(key)


async def _undersized(run: SandboxRun, report: ProbeReport, quote: Quote) -> None:
    try:
        key, order = await run.submit(OrderType.LIMIT, UNDERSIZED, _price(quote.bid / 2))
    except ProviderOrderRejectedError as exc:
        report.step("undersized_rejection", "pass", reason=str(exc))
        return
    report.step("undersized_rejection", "fail", status=order.status.value)


async def _partial_fill(run: SandboxRun, report: ProbeReport) -> None:
    for attempt in range(BOOK_READS):
        if attempt:
            await asyncio.sleep(BOOK_PAUSE_SECONDS)
        # One level per side: a limit of 0 would return Gemini's whole side of the book.
        response = await run.client.get(
            f"{GEMINI_SANDBOX_REST_URL}/v1/book/btcusd", params={"limit_bids": 1, "limit_asks": 1}
        )
        try:
            best = response.json()["asks"][0]
            price, depth = Decimal(str(best["price"])), Decimal(str(best["amount"]))
        except (ValueError, KeyError, IndexError, TypeError, ArithmeticError):
            report.step(
                "partial_fill", "skipped", reason="the Sandbox order book had no readable ask"
            )
            return
        size = depth + PROBE_SIZE
        if size <= PARTIAL_FILL_CAP:
            break
    else:
        report.step(
            "partial_fill",
            "skipped",
            reason=(
                f"on {BOOK_READS} reads the best ask held more than the probe's "
                f"{PARTIAL_FILL_CAP} BTC cap"
            ),
        )
        return
    # Buying a little more than the best ask holds should fill part and rest the rest.
    key, order = await run.submit(OrderType.LIMIT, size, _price(price))
    fills = await run.broker.get_fills(key)
    report.step(
        "partial_fill",
        _check(order.status is OrderStatus.PARTIALLY_FILLED),
        status=order.status.value,
        fills=len(fills),
    )
    if key in run.resting:
        await run.broker.cancel_order(key)
        after = await run.broker.get_order(key)
        if after is not None and after.status not in LIVE:
            run.resting.remove(key)


async def _trading_loop(run: SandboxRun, report: ProbeReport) -> None:
    candles = await _closed_candles(run.client)
    if len(candles) < 2:
        report.step("trading_loop", "skipped", reason="the Sandbox returned too few closed bars")
        return
    last = candles[-1]
    state = MarketState(
        symbol=SYMBOL,
        quote=Quote(
            symbol=SYMBOL,
            bid=last.close,
            ask=last.close,
            as_of=last.closed_at,
            source="gemini-sandbox",
            received_at=last.closed_at,
        ),
        candles=tuple(candles),
        observed_at=last.closed_at,
    )
    left_out = await _unpriced_holdings(run.broker)
    if left_out:
        report.notes.append(
            f"The trading-loop step left out {', '.join(left_out)}: the Sandbox has no buyers "
            "for them, so they cannot be priced. The trading bot itself still refuses to buy "
            "while any holding cannot be priced."
        )
    broker = PricedHoldingsView(run.broker, frozenset(left_out))
    store = InMemoryOrderStore()
    audit = InMemoryAuditStore(store)
    cycle = TradingCycle(
        strategy=AlwaysBuyStrategy(PROBE_SIZE),
        execution=ExecutionEngine(broker, store),
        audit=audit,
        kill_switch=KillSwitch(),
        risk_inputs=BrokerRiskInputs(
            broker, constraints=CONSTRAINTS, estimated_slippage=Decimal("0.001")
        ),
        limits=PROBE_LIMITS,
        environ={},
    )
    outcome = await cycle.on_market_state(state)
    if outcome.order is not None and outcome.order.status in LIVE:
        run.resting.append(str(outcome.order.request.client_order_id))
    gaps = sorted({gap for item in unlinked_orders(audit) for gap in item.gaps})
    submitted = outcome.status is CycleStatus.SUBMITTED
    report.step(
        "trading_loop",
        "pass" if submitted and not gaps else "refused",
        status=outcome.status.value,
        detail=outcome.detail,
        failed_gate=outcome.decision.failed_gate if outcome.decision else None,
        order_status=outcome.order.status.value if outcome.order else None,
        lineage_gaps=gaps,
        left_out=left_out,
    )


async def _unpriced_holdings(broker: GeminiBroker) -> list[str]:
    """Held symbols, other than the traded one, that the Sandbox cannot price.

    The rule matches how the trading loop values other holdings: a holding whose
    quote cannot be read cannot be priced, and a quote without a positive bid cannot
    be read (Gemini sends an empty bid when a book has no buyers). The probe's own
    budget and host refusals are never taken for an unpriced holding.
    """

    unpriced = []
    for position in await broker.get_positions():
        if position.symbol == SYMBOL:
            continue
        try:
            await broker.get_quote(position.symbol)
        except (BudgetExhausted, UnexpectedHost):
            raise
        except Exception:
            unpriced.append(position.symbol)
    return sorted(unpriced)


async def _closed_candles(client: httpx.AsyncClient) -> list[Candle]:
    response = await client.get(f"{GEMINI_SANDBOX_REST_URL}/v2/candles/btcusd/5m")
    now = datetime.now(UTC)
    candles = []
    try:
        rows: list[Any] = response.json()
        for row in rows:
            opened = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            if opened + BAR > now:
                continue  # still in progress
            candles.append(
                Candle(
                    symbol=SYMBOL,
                    interval="FIVE_MINUTE",
                    opened_at=opened,
                    closed_at=opened + BAR,
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[5])),
                    source="gemini-sandbox",
                    as_of=opened + BAR,
                )
            )
    except (ValueError, KeyError, IndexError, TypeError, ArithmeticError):
        return []
    # Gemini lists the newest bar first; the loop expects oldest first, last 50 bars.
    return sorted(candles, key=lambda candle: candle.opened_at)[-50:]


async def _cancel_leftovers(
    run: SandboxRun, report: ProbeReport, transport: BoundedTransport
) -> None:
    """Cancel anything still resting, even after a failure, through a fresh adapter."""

    transport.release_reserve()
    if not run.resting:
        return
    broker = run.restart()
    left: list[str] = []
    for key in list(run.resting):
        try:
            order = await broker.get_order(key)
            if order is not None and order.status in LIVE:
                await broker.cancel_order(key)
                order = await broker.get_order(key)
            if order is not None and order.status in LIVE:
                left.append(key)
        except Exception:
            left.append(key)
    if left:
        report.step("cleanup", "fail", still_resting=len(left))
        report.notes.append(
            f"{len(left)} Sandbox order(s) may still be resting; cancel them on the Gemini "
            "Sandbox website."
        )
    else:
        report.step("cleanup", "pass", canceled=len(run.resting))
    run.resting = left


def _price(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


def _check(ok: bool) -> str:
    return "pass" if ok else "fail"


def _describe(exc: Exception) -> dict[str, Any]:
    """The error type, HTTP status, and Gemini's reason code (e.g. ``InvalidSignature``)."""

    details: dict[str, Any] = {"error": type(exc).__name__}
    # An adapter rejection wraps Gemini's HTTP error, which carries the status and reason.
    source = exc.__cause__ if isinstance(exc, ProviderOrderRejectedError) and exc.__cause__ else exc
    status = getattr(source, "status_code", None)
    if status:
        details["http_status"] = status
    payload = getattr(source, "payload", None)
    reason = payload.get("reason") if isinstance(payload, dict) else None
    if not reason and isinstance(exc, ProviderOrderRejectedError):
        reason = str(exc)
    if isinstance(reason, str) and reason:
        details["reason"] = reason[:64]
    return details


async def _prompted() -> ProbeReport:
    print("Gemini Sandbox check. Use a Sandbox key with the Trader role only.")
    api_key = prompt_secret("Sandbox API key")
    api_secret = prompt_secret("Sandbox API secret")
    return await lifecycle(api_key, api_secret)


if __name__ == "__main__":  # pragma: no cover - the owner runs this from a terminal
    raise SystemExit(run_probe(_prompted))
