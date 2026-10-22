"""#170: each traded symbol has its own order quantity and its own exchange limits.

Until now one ``PAPER_ORDER_QUANTITY`` and one set of increments served every symbol, so a
second coin would have been sized and checked with BTC's numbers. These tests pin the
opposite: a symbol uses only its own numbers, one without a complete set is refused, and a
single-symbol setup behaves exactly as it did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from app.paper_runtime import PaperRuntimeConfig, resolve_constraints
from app.trading import BrokerRiskInputs
from brokers.gemini import GeminiBroker
from brokers.http import ProviderHTTPError
from brokers.interface import SymbolRules
from brokers.simulated import SimulatedBroker
from core.guards import StartupGuardError
from core.models import MarketState, OrderSide, Quote, Signal
from core.resilience import TokenBucketRateLimiter
from pydantic import ValidationError
from risk.engine import ExchangeConstraints, evaluate
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import CONSTRAINTS, PARITY_LIMITS
from tests.test_trading_cycle import STATE, StubBroker, context

BTC = "BTC-USD"
ETH = "ETH-USD"
TWO = {
    "PAPER_SYMBOLS": "BTC-USD,ETH-USD",
    "PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001,ETH-USD:0.01",
}
BTC_RULES = SymbolRules(
    symbol=BTC,
    min_quantity=Decimal("0.00001"),
    quantity_increment=Decimal("0.00000001"),
    price_increment=Decimal("0.01"),
)
ETH_RULES = SymbolRules(
    symbol=ETH,
    min_quantity=Decimal("0.001"),
    quantity_increment=Decimal("0.000001"),
    price_increment=Decimal("0.01"),
)


class PublishedRules(SimulatedBroker):
    """A broker that publishes limits for the symbols it is given, as a venue would."""

    def __init__(self, rules: dict[str, SymbolRules | Exception | None]) -> None:
        super().__init__(
            Quote(
                symbol=BTC, bid=Decimal("1"), ask=Decimal("1"), as_of=datetime.now(UTC), source="t"
            )
        )
        self.rules = rules
        self.asked: list[str] = []

    async def get_symbol_rules(self, symbol: str) -> SymbolRules | None:
        self.asked.append(symbol)
        outcome = self.rules.get(symbol)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- configuration -------------------------------------------------------------------------


def test_a_single_symbol_with_no_new_settings_keeps_todays_quantity() -> None:
    config = PaperRuntimeConfig.from_env({})

    assert config.order_quantities == {BTC: Decimal("0.0001")}
    assert config.min_notionals == {BTC: Decimal("1")}
    assert config.venue_rules is False
    # The compose files pass the new settings through empty; that must mean "unset".
    blank = PaperRuntimeConfig.from_env({"PAPER_ORDER_QUANTITIES": " ", "PAPER_MIN_NOTIONALS": ""})
    assert blank == config


def test_each_symbol_takes_its_own_quantity_and_minimum_order_value() -> None:
    config = PaperRuntimeConfig.from_env(
        {**TWO, "PAPER_MIN_NOTIONAL": "2", "PAPER_MIN_NOTIONALS": "eth-usd:5"}
    )

    assert config.venue_rules is True
    assert config.order_quantities == {BTC: Decimal("0.0001"), ETH: Decimal("0.01")}
    assert config.min_notionals == {BTC: Decimal("2"), ETH: Decimal("5")}


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"PAPER_SYMBOLS": "BTC-USD,ETH-USD"}, "PAPER_ORDER_QUANTITIES must give each"),
        (
            {"PAPER_SYMBOLS": "BTC-USD,ETH-USD", "PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001"},
            "no quantity for ETH-USD",
        ),
        (
            {"PAPER_SYMBOLS": "BTC-USD,ETH-USD", "PAPER_ORDER_QUANTITY": "0.0001"},
            "PAPER_ORDER_QUANTITIES must give each",
        ),
        (
            {**TWO, "PAPER_ORDER_QUANTITIES": TWO["PAPER_ORDER_QUANTITIES"] + ",SOL-USD:1"},
            "SOL-USD",
        ),
        ({**TWO, "PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001,BTC-USD:0.0002"}, "more than once"),
        ({**TWO, "PAPER_ORDER_QUANTITIES": "BTC-USD=0.0001,ETH-USD:0.01"}, "must look like"),
        ({**TWO, "PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001,ETH-USD:0"}, "positive decimal"),
        ({**TWO, "PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001,ETH-USD:NaN"}, "finite"),
        ({**TWO, "PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001,ETH-USD:lots"}, "must be a decimal"),
        ({**TWO, "PAPER_MIN_NOTIONALS": "XRP-USD:1"}, "XRP-USD"),
    ],
)
def test_a_symbol_without_a_complete_quantity_refuses_to_start(environ, message) -> None:
    with pytest.raises(StartupGuardError, match=message):
        PaperRuntimeConfig.from_env(environ)


# --- the strategy --------------------------------------------------------------------------


def test_the_strategy_signals_each_symbol_with_its_own_quantity_and_skips_unknown_ones() -> None:
    quantities = {BTC: Decimal("0.0001"), ETH: Decimal("0.01")}
    strategy = MovingAverageCrossStrategy(quantity=quantities)
    eth_state = _renamed(STATE, ETH)

    btc_signal = strategy.on_market_state(STATE)
    eth_signal = strategy.on_market_state(eth_state)

    assert btc_signal is not None and btc_signal.quantity == Decimal("0.0001")
    assert eth_signal is not None and eth_signal.quantity == Decimal("0.01")
    assert strategy.on_market_state(_renamed(STATE, "SOL-USD")) is None


def test_the_single_symbol_default_makes_the_same_signal_as_before() -> None:
    before = MovingAverageCrossStrategy(quantity=Decimal("0.0001"))
    after = MovingAverageCrossStrategy(quantity={BTC: Decimal("0.0001")})

    assert after.strategy_version == before.strategy_version
    assert after.on_market_state(STATE) == before.on_market_state(STATE)
    assert MovingAverageCrossStrategy().on_market_state(STATE) is not None  # scalar form kept


def test_the_strategy_refuses_an_empty_or_non_positive_quantity_map() -> None:
    with pytest.raises(ValueError, match="at least one symbol"):
        MovingAverageCrossStrategy(quantity={})
    with pytest.raises(ValueError, match="positive"):
        MovingAverageCrossStrategy(quantity={BTC: Decimal("0.0001"), ETH: Decimal("0")})


# --- the risk inputs -----------------------------------------------------------------------


class PerSymbolQuotes(StubBroker):
    async def get_quote(self, symbol):
        price = STATE.candles[-1].close
        return Quote(symbol=symbol, bid=price, ask=price, as_of=datetime.now(UTC), source="unit")


def _signal(symbol: str, quantity: str) -> Signal:
    return Signal(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        strategy_version="per-symbol-v1",
    )


@pytest.mark.asyncio
async def test_each_signal_is_checked_against_its_own_symbols_limits() -> None:
    by_symbol = {
        BTC: CONSTRAINTS,
        ETH: ExchangeConstraints(
            min_quantity=Decimal("0.001"),
            quantity_increment=Decimal("0.000001"),
            min_notional=Decimal("1"),
            price_increment=Decimal("0.01"),
        ),
    }
    source = BrokerRiskInputs(
        PerSymbolQuotes(), constraints=by_symbol, estimated_slippage=Decimal("0")
    )

    btc = await source.risk_inputs(_signal(BTC, "0.0001"), STATE, context())
    eth = await source.risk_inputs(_signal(ETH, "0.0005"), _renamed(STATE, ETH), context())

    assert btc.constraints == by_symbol[BTC]
    assert eth.constraints == by_symbol[ETH]
    # 0.0005 clears BTC's limits but is under ETH's minimum, so only ETH refuses it.
    assert evaluate(_signal(BTC, "0.0005"), btc, PARITY_LIMITS).approved
    refused = evaluate(_signal(ETH, "0.0005"), eth, PARITY_LIMITS)
    assert not refused.approved and refused.failed_gate == "exchange_constraints"
    assert refused.reason == "quantity is below the exchange minimum"


@pytest.mark.asyncio
async def test_a_symbol_without_limits_is_refused_and_never_borrows_another_symbols() -> None:
    source = BrokerRiskInputs(
        PerSymbolQuotes(), constraints={BTC: CONSTRAINTS}, estimated_slippage=Decimal("0")
    )

    inputs = await source.risk_inputs(_signal(ETH, "0.01"), _renamed(STATE, ETH), context())

    assert inputs.constraints is None
    decision = evaluate(_signal(ETH, "0.01"), inputs, PARITY_LIMITS)
    assert not decision.approved and decision.failed_gate == "exchange_constraints"
    assert decision.reason == "current exchange constraints are unavailable"


@pytest.mark.asyncio
async def test_one_constraints_object_still_serves_every_symbol() -> None:
    source = BrokerRiskInputs(
        PerSymbolQuotes(), constraints=CONSTRAINTS, estimated_slippage=Decimal("0")
    )

    inputs = await source.risk_inputs(_signal(BTC, "0.0001"), STATE, context())

    assert inputs.constraints == CONSTRAINTS


# --- startup resolution --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_single_symbol_default_resolves_to_todays_constraints_without_a_venue_read() -> (
    None
):
    broker = PublishedRules({})

    resolved = await resolve_constraints(PaperRuntimeConfig.from_env({}), broker)

    assert resolved == {
        BTC: ExchangeConstraints(
            min_quantity=Decimal("0.00000001"),
            quantity_increment=Decimal("0.00000001"),
            min_notional=Decimal("1"),
            price_increment=Decimal("0.01"),
        )
    }
    assert broker.asked == []


@pytest.mark.asyncio
async def test_the_single_symbol_default_still_refuses_a_quantity_off_the_broker_step() -> None:
    config = PaperRuntimeConfig.from_env({"PAPER_ORDER_QUANTITY": "0.000000005"})

    with pytest.raises(StartupGuardError, match="PAPER_ORDER_QUANTITY does not match"):
        await resolve_constraints(config, PublishedRules({}))


@pytest.mark.asyncio
async def test_two_symbols_resolve_to_their_own_venue_limits() -> None:
    broker = PublishedRules({BTC: BTC_RULES, ETH: ETH_RULES})
    config = PaperRuntimeConfig.from_env({**TWO, "PAPER_MIN_NOTIONALS": "ETH-USD:5"})

    resolved = await resolve_constraints(config, broker)

    assert resolved[BTC].min_quantity == Decimal("0.00001")
    assert resolved[BTC].min_notional == Decimal("1")
    assert resolved[ETH].min_quantity == Decimal("0.001")
    assert resolved[ETH].quantity_increment == Decimal("0.000001")
    assert resolved[ETH].min_notional == Decimal("5")
    assert resolved[BTC] != resolved[ETH]


@pytest.mark.asyncio
async def test_the_venues_own_minimum_order_value_wins_when_it_is_stricter() -> None:
    strict = ETH_RULES.model_copy(update={"min_notional": Decimal("10")})
    config = PaperRuntimeConfig.from_env({**TWO, "PAPER_MIN_NOTIONAL": "2"})

    resolved = await resolve_constraints(config, PublishedRules({BTC: BTC_RULES, ETH: strict}))

    assert resolved[ETH].min_notional == Decimal("10")
    assert resolved[BTC].min_notional == Decimal("2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantities", "message"),
    [
        ("BTC-USD:0.0001,ETH-USD:0.0005", "ETH-USD does not match"),  # under ETH's minimum
        ("BTC-USD:0.0001,ETH-USD:0.0010005", "ETH-USD does not match"),  # off ETH's step
        ("BTC-USD:0.000005,ETH-USD:0.01", "BTC-USD does not match"),
    ],
)
async def test_a_quantity_that_does_not_fit_its_own_symbols_step_is_refused(
    quantities: str, message: str
) -> None:
    config = PaperRuntimeConfig.from_env({**TWO, "PAPER_ORDER_QUANTITIES": quantities})

    with pytest.raises(StartupGuardError, match=message):
        await resolve_constraints(config, PublishedRules({BTC: BTC_RULES, ETH: ETH_RULES}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", [None, RuntimeError("venue down")], ids=["no rules published", "read failed"]
)
async def test_a_symbol_whose_rules_are_unavailable_refuses_start_and_does_not_use_btcs(
    outcome,
) -> None:
    broker = PublishedRules({BTC: BTC_RULES, ETH: outcome})

    with pytest.raises(StartupGuardError, match="ETH-USD"):
        await resolve_constraints(PaperRuntimeConfig.from_env(TWO), broker)


@pytest.mark.asyncio
async def test_a_broker_that_publishes_no_rules_refuses_a_venue_rules_start() -> None:
    plain = SimulatedBroker(
        Quote(symbol=BTC, bid=Decimal("1"), ask=Decimal("1"), as_of=datetime.now(UTC), source="t")
    )
    config = PaperRuntimeConfig.from_env({"PAPER_ORDER_QUANTITIES": "BTC-USD:0.0001"})

    with pytest.raises(StartupGuardError, match="no usable order rules for BTC-USD"):
        await resolve_constraints(config, plain)


def test_symbol_rules_reject_a_non_positive_limit() -> None:
    with pytest.raises(ValidationError):
        SymbolRules.model_validate({**BTC_RULES.model_dump(), "min_quantity": Decimal("0")})
    with pytest.raises(ValidationError):
        SymbolRules.model_validate({**BTC_RULES.model_dump(), "min_notional": Decimal("-1")})


# --- the Gemini adapter --------------------------------------------------------------------

SANDBOX_DETAILS = {
    "btcusd": {
        "symbol": "BTCUSD",
        "base_currency": "BTC",
        "quote_currency": "USD",
        "tick_size": 0.00000001,
        "quote_increment": 0.01,
        "min_order_size": "0.00001",
        "status": "open",
    },
    "ethusd": {
        "symbol": "ETHUSD",
        "base_currency": "ETH",
        "quote_currency": "USD",
        "tick_size": 0.000001,
        "quote_increment": 0.01,
        "min_order_size": "0.001",
        "status": "open",
    },
    "closedusd": {
        "symbol": "CLOSEDUSD",
        "tick_size": 0.1,
        "quote_increment": 0.1,
        "min_order_size": "1",
        "status": "closed",
    },
    "wrongusd": {
        "symbol": "ELSEUSD",
        "tick_size": 0.1,
        "quote_increment": 0.1,
        "min_order_size": "1",
        "status": "open",
    },
}


def _gemini_with_details() -> tuple[GeminiBroker, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        prefix = "/v1/symbols/details/"
        name = request.url.path.removeprefix(prefix)
        if request.url.path.startswith(prefix) and name in SANDBOX_DETAILS:
            return httpx.Response(200, json=SANDBOX_DETAILS[name])
        return httpx.Response(404, json={"result": "error", "reason": "NoSuchSymbol"})

    broker = GeminiBroker(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(10_000, 10_000),
    )
    return broker, seen


@pytest.mark.asyncio
async def test_gemini_reads_each_symbols_limits_from_its_public_symbol_details() -> None:
    broker, seen = _gemini_with_details()

    btc = await broker.get_symbol_rules(BTC)
    eth = await broker.get_symbol_rules(ETH)

    assert btc == BTC_RULES
    assert eth == ETH_RULES
    assert btc is not None and btc.min_notional is None  # Gemini publishes no order minimum
    # A public read: GET only, on the Sandbox host, with no credentials or signed payload.
    assert [(r.method, r.url.host, r.url.path) for r in seen] == [
        ("GET", "api.sandbox.gemini.com", "/v1/symbols/details/btcusd"),
        ("GET", "api.sandbox.gemini.com", "/v1/symbols/details/ethusd"),
    ]
    assert all("x-gemini-apikey" not in r.headers for r in seen)
    await broker.close()


@pytest.mark.asyncio
async def test_gemini_gives_no_rules_for_a_symbol_that_is_not_open() -> None:
    broker, _ = _gemini_with_details()

    assert await broker.get_symbol_rules("CLOSED-USD") is None
    await broker.close()


@pytest.mark.asyncio
async def test_gemini_fails_when_the_details_name_another_symbol_or_the_symbol_is_unknown() -> None:
    broker, _ = _gemini_with_details()

    with pytest.raises(ProviderHTTPError):
        await broker.get_symbol_rules("WRONG-USD")
    with pytest.raises(ProviderHTTPError):
        await broker.get_symbol_rules("NOPE-USD")
    await broker.close()


def _renamed(state: MarketState, symbol: str) -> MarketState:
    """The same closed bars and quote under another symbol, so only the symbol differs."""

    return state.model_copy(
        update={
            "symbol": symbol,
            "quote": state.quote.model_copy(update={"symbol": symbol}) if state.quote else None,
            "candles": tuple(c.model_copy(update={"symbol": symbol}) for c in state.candles),
        }
    )
