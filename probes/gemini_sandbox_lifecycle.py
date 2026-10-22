"""Owner-run: the Gemini Sandbox order lifecycle and one trading-loop pass.

Everything goes through ``GeminiBroker``, which refuses any host outside
``*.sandbox.gemini.com``, so no production account can be reached. Orders are
tiny, the request budget is fixed, and any order still resting at the end is
canceled in ``finally``. The Sandbox key must be created for the Primary account
(a group-level Master key is refused) with the Trader role only, and the account
needs test US dollars, which the first step checks before any order.

Run from the repository root, confirm the account in the typed prompt, and type the
key and secret into the hidden prompts::

    python -m probes.gemini_sandbox_lifecycle
    python -m probes.gemini_sandbox_lifecycle --symbol ETH-USD

``--symbol`` accepts only the symbols in ``PROFILES``; anything else is refused before
a request is made, and a symbol's orders use that symbol's own small size. The run uses
the account whose key you enter and leaves orders waiting for a few seconds, so it asks
you to type that this is not the account the paper app trades on.

The redacted result records statuses, rejection reasons, and counts: no key,
order identifier, or balance. It records the symbol's published order limits, how many
decimals the Sandbox reports for the coin and for US dollars, and whether our balance
projection differs from the Sandbox's after the first order. It names any holding the
trading-loop step left out because the Sandbox could not price it.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any
from uuid import uuid4

import httpx
from app.trading import BrokerRiskInputs, CycleStatus, TradingCycle
from brokers.gemini import GEMINI_SANDBOX_REST_URL, GeminiBroker, _gemini_symbol
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
from portfolio.ledger import apply_fills
from portfolio.reconciliation import PortfolioState
from risk.engine import ExchangeConstraints, RiskLimits
from risk.kill_switch import KillSwitch
from strategy.reference import AlwaysBuyStrategy

from probes.common import (
    BoundedTransport,
    BudgetExhausted,
    ProbeRefused,
    ProbeReport,
    UnexpectedHost,
    bounded_client,
    prompt_secret,
    require_typed,
    run_probe,
)

SANDBOX_HOST = "api.sandbox.gemini.com"
DEFAULT_SYMBOL = "BTC-USD"
# About 35 requests, plus two quotes per Sandbox currency when the loop values holdings.
REQUEST_BUDGET = 80
CLEANUP_RESERVE = 10
# The Sandbox's best ask changes from moment to moment, so read it a few times.
BOOK_READS = 5
BOOK_PAUSE_SECONDS = 2.0
CENT = Decimal("0.01")
BAR = timedelta(minutes=5)
ACCOUNT_STATEMENT = "this is not the account the paper app trades on"


@dataclass(frozen=True)
class SymbolProfile:
    """One symbol the check may trade: its order sizes and the limits the app assumes.

    The ``min_quantity``, ``quantity_increment`` and ``price_increment`` are what the check
    (and so the app's order sizing) assumes for the symbol; the ``symbol_details`` step reads
    Gemini's own values and reports any difference. ``probe_size`` sits well above the minimum,
    ``undersized`` well below it, and ``partial_fill_cap`` bounds the one deliberate partial fill.
    """

    symbol: str
    probe_size: Decimal
    undersized: Decimal
    partial_fill_cap: Decimal
    min_quantity: Decimal
    quantity_increment: Decimal
    price_increment: Decimal = CENT

    @property
    def base(self) -> str:
        return self.symbol.split("-", maxsplit=1)[0]

    @property
    def venue_symbol(self) -> str:
        return _gemini_symbol(self.symbol)

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_quantity=self.min_quantity,
            quantity_increment=self.quantity_increment,
            min_notional=CENT,
            price_increment=self.price_increment,
        )


PROFILES: dict[str, SymbolProfile] = {
    "BTC-USD": SymbolProfile(
        symbol="BTC-USD",
        probe_size=Decimal("0.0001"),  # well above the documented 0.00001 minimum
        undersized=Decimal("0.000001"),
        partial_fill_cap=Decimal("0.01"),
        min_quantity=Decimal("0.00001"),
        quantity_increment=Decimal("0.00000001"),
    ),
    # Gemini's published ETHUSD limits (0.001 smallest order, 0.000001 step), not yet seen
    # in this Sandbox; the ``symbol_details`` step records what it reports.
    "ETH-USD": SymbolProfile(
        symbol="ETH-USD",
        probe_size=Decimal("0.01"),
        undersized=Decimal("0.0001"),
        partial_fill_cap=Decimal("0.2"),
        min_quantity=Decimal("0.001"),
        quantity_increment=Decimal("0.000001"),
    ),
}


def profile_for(symbol: str) -> SymbolProfile:
    """The profile for ``symbol``; any other symbol is refused before a request is made."""

    profile = PROFILES.get(symbol)
    if profile is None:
        raise ProbeRefused(
            f"{symbol!r} is not a symbol this check may trade; choose one of {sorted(PROFILES)}"
        )
    return profile


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

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient,
        profile: SymbolProfile | None = None,
    ) -> None:
        self._credentials = (api_key, api_secret)
        self.client = client
        self.profile = profile or PROFILES[DEFAULT_SYMBOL]
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
            symbol=self.profile.symbol,
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
    symbol: str = DEFAULT_SYMBOL,
    inner: httpx.AsyncBaseTransport | None = None,
) -> ProbeReport:
    profile = profile_for(symbol)  # before anything is built, so no request can be made
    name = "gemini-sandbox-lifecycle"
    if profile.symbol != DEFAULT_SYMBOL:
        name = f"{name}-{profile.symbol.lower()}"  # a separate result file per symbol
    report = ProbeReport(probe=name, target=GEMINI_SANDBOX_REST_URL)
    report.keep_out(api_key, api_secret)
    client, transport = bounded_client(
        hosts={SANDBOX_HOST}, budget=REQUEST_BUDGET, reserve=CLEANUP_RESERVE, inner=inner
    )
    run = SandboxRun(api_key, api_secret, client, profile)
    try:
        quote = await _funded_quote(run, report)
        if quote is not None and await _symbol_details(run, report):
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
    profile = run.profile
    quote = await run.broker.get_quote(profile.symbol)
    usd = next((item.available for item in balances if item.asset == "USD"), Decimal("0"))
    # Two capped buys, one resting buy, and a partial fill of up to the cap, plus fees.
    needed = (
        _price(quote.ask * (3 * profile.probe_size + profile.partial_fill_cap) * Decimal("1.03"))
        + CENT
    )
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


async def _symbol_details(run: SandboxRun, report: ProbeReport) -> bool:
    """Read the symbol's published limits (public, no key) and compare them with the check's.

    Returns ``False`` when the symbol is not open, so the run stops before placing anything.
    The limits are public exchange rules, not account data, so their values are recorded.
    """

    profile = run.profile
    rules = await run.broker.get_symbol_rules(profile.symbol)
    if rules is None:
        report.step("symbol_details", "fail", reason="Gemini does not list the symbol as open")
        return False
    venue = {
        "min_quantity": rules.min_quantity,
        "quantity_increment": rules.quantity_increment,
        "price_increment": rules.price_increment,
    }
    assumed = {
        "min_quantity": profile.min_quantity,
        "quantity_increment": profile.quantity_increment,
        "price_increment": profile.price_increment,
    }
    differs = sorted(name for name, value in venue.items() if value != assumed[name])
    report.step(
        "symbol_details",
        "differs" if differs else "pass",
        symbol=profile.symbol,
        **{name: _plain(value) for name, value in venue.items()},
        differs_from_assumed=differs,
    )
    if differs:
        report.notes.append(
            f"Gemini's {profile.symbol} limits differ from the ones this check assumes "
            f"({', '.join(differs)}); the assumed values are in probes/gemini_sandbox_lifecycle.py."
        )
    return True


async def _capped_market_order(run: SandboxRun, report: ProbeReport) -> None:
    """A market request goes out as an immediate-or-cancel limit capped 1% past the ask."""

    before = await run.broker.get_balances()
    try:
        key, order = await run.submit(OrderType.MARKET, run.profile.probe_size)
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
    await _balance_precision(run, report, before, fills)


async def _balance_precision(
    run: SandboxRun, report: ProbeReport, before: tuple[Balance, ...], fills: tuple[Fill, ...]
) -> None:
    """Project the balances the way the app does, then compare them with the Sandbox's.

    The projection is ``apply_fills`` with the adapter's declared balance units, the rule the
    paper app reconciles with. Only the number of decimals the Sandbox reports per asset and
    the broker-minus-projected differences are recorded, never a balance.
    """

    profile = run.profile
    increments = run.broker.capabilities.balance_increments
    projected = apply_fills(
        PortfolioState(balances=before), fills, balance_increments=increments
    ).balances
    after = await run.broker.get_balances()
    assets = sorted({profile.base, "USD", *(fill.fee_asset for fill in fills)})
    per_asset: dict[str, dict[str, Any]] = {}
    for asset in assets:
        expected = next((item for item in projected if item.asset == asset), None)
        actual = next((item for item in after if item.asset == asset), None)
        zero = Decimal("0")
        exp_available, exp_hold = (expected.available, expected.hold) if expected else (zero, zero)
        got_available, got_hold = (actual.available, actual.hold) if actual else (zero, zero)
        unit = increments.get(asset)
        per_asset[asset] = {
            "broker_decimals": _decimals(actual.available) if actual else None,
            "app_unit": None if unit is None else _plain(unit),
            "available_difference": _plain(got_available - exp_available),
            "hold_difference": _plain(got_hold - exp_hold),
        }
    clean = all(
        entry["available_difference"] == "0" and entry["hold_difference"] == "0"
        for entry in per_asset.values()
    )
    report.step("balance_precision", "pass" if clean else "differs", assets=per_asset)
    if not clean:
        report.notes.append(
            "After the first order the Sandbox's balance differs from the app's projection "
            "(broker minus projected is listed per asset). The paper app would halt on this "
            "difference; it is a finding for its own card."
        )


async def _resting_limit_and_recovery(run: SandboxRun, report: ProbeReport, quote: Quote) -> None:
    price = _price(quote.bid / 2)  # far below the market, so the order rests
    key, order = await run.submit(OrderType.LIMIT, run.profile.probe_size, price)
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
        key, order = await run.submit(
            OrderType.LIMIT, run.profile.undersized, _price(quote.bid / 2)
        )
    except ProviderOrderRejectedError as exc:
        report.step("undersized_rejection", "pass", reason=str(exc))
        return
    report.step("undersized_rejection", "fail", status=order.status.value)


async def _partial_fill(run: SandboxRun, report: ProbeReport) -> None:
    profile = run.profile
    for attempt in range(BOOK_READS):
        if attempt:
            await asyncio.sleep(BOOK_PAUSE_SECONDS)
        # One level per side: a limit of 0 would return Gemini's whole side of the book.
        response = await run.client.get(
            f"{GEMINI_SANDBOX_REST_URL}/v1/book/{profile.venue_symbol}",
            params={"limit_bids": 1, "limit_asks": 1},
        )
        try:
            best = response.json()["asks"][0]
            price, depth = Decimal(str(best["price"])), Decimal(str(best["amount"]))
        except (ValueError, KeyError, IndexError, TypeError, ArithmeticError):
            report.step(
                "partial_fill", "skipped", reason="the Sandbox order book had no readable ask"
            )
            return
        size = depth + profile.probe_size
        if size <= profile.partial_fill_cap:
            break
    else:
        report.step(
            "partial_fill",
            "skipped",
            reason=(
                f"on {BOOK_READS} reads the best ask held more than the probe's "
                f"{profile.partial_fill_cap} {profile.base} cap"
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
    profile = run.profile
    candles = await _closed_candles(run.client, profile)
    if len(candles) < 2:
        report.step("trading_loop", "skipped", reason="the Sandbox returned too few closed bars")
        return
    last = candles[-1]
    state = MarketState(
        symbol=profile.symbol,
        quote=Quote(
            symbol=profile.symbol,
            bid=last.close,
            ask=last.close,
            as_of=last.closed_at,
            source="gemini-sandbox",
            received_at=last.closed_at,
        ),
        candles=tuple(candles),
        observed_at=last.closed_at,
    )
    left_out = await _unpriced_holdings(run.broker, profile.symbol)
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
        strategy=AlwaysBuyStrategy(profile.probe_size),
        execution=ExecutionEngine(broker, store),
        audit=audit,
        kill_switch=KillSwitch(),
        risk_inputs=BrokerRiskInputs(
            broker, constraints=profile.constraints, estimated_slippage=Decimal("0.001")
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


async def _unpriced_holdings(broker: GeminiBroker, traded: str) -> list[str]:
    """Held symbols, other than the traded one, that the Sandbox cannot price.

    The rule matches how the trading loop values other holdings: a holding whose
    quote cannot be read cannot be priced, and a quote without a positive bid cannot
    be read (Gemini sends an empty bid when a book has no buyers). The probe's own
    budget and host refusals are never taken for an unpriced holding.
    """

    unpriced = []
    for position in await broker.get_positions():
        if position.symbol == traded:
            continue
        try:
            await broker.get_quote(position.symbol)
        except (BudgetExhausted, UnexpectedHost):
            raise
        except Exception:
            unpriced.append(position.symbol)
    return sorted(unpriced)


async def _closed_candles(client: httpx.AsyncClient, profile: SymbolProfile) -> list[Candle]:
    response = await client.get(f"{GEMINI_SANDBOX_REST_URL}/v2/candles/{profile.venue_symbol}/5m")
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
                    symbol=profile.symbol,
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


def _plain(value: Decimal) -> str:
    """A decimal without an exponent or trailing zeros: ``0.00002``, and ``0`` for zero."""

    return format(value.normalize(), "f")


def _decimals(value: Decimal) -> int:
    """How many decimals the venue wrote the number with, trailing zeros included."""

    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


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


async def _prompted(symbol: str = DEFAULT_SYMBOL) -> ProbeReport:
    profile = profile_for(symbol)  # a symbol this check may not trade stops here
    print(
        f"Gemini Sandbox check for {profile.symbol}. Use a Sandbox key with the Trader role only."
    )
    print(
        "It buys on the account whose key you enter and leaves orders waiting for a few "
        "seconds. If the paper app trades on that account, its next reconciliation will halt."
    )
    require_typed(ACCOUNT_STATEMENT)
    api_key = prompt_secret("Sandbox API key")
    api_secret = prompt_secret("Sandbox API secret")
    return await lifecycle(api_key, api_secret, symbol=profile.symbol)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m probes.gemini_sandbox_lifecycle")
    parser.add_argument("--symbol", choices=sorted(PROFILES), default=DEFAULT_SYMBOL)
    args = parser.parse_args(argv)
    return run_probe(lambda: _prompted(args.symbol))


if __name__ == "__main__":  # pragma: no cover - the owner runs this from a terminal
    raise SystemExit(main())
