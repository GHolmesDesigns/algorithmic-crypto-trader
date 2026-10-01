"""Bounded operational history: read-only, capped, indexed, and truthful about absence."""

import inspect
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from api.history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SqlAlchemyHistory,
    parse_query,
)
from api.history_routes import router as history_router
from api.history_view import PATHS
from app.main import create_app
from app.trading import CycleStatus
from brokers.simulated import SimulatedBroker
from core.models import OrderStatus, Quote, utc_now
from db.models import (
    DiscrepancyRecord,
    FillRecord,
    OrderRecord,
    RiskDecisionRecord,
    SignalRecord,
    SystemEventRecord,
)
from execution.audit import SqlAlchemyAuditStore
from execution.persistence import SqlAlchemyOrderStore
from risk.engine import RISK_GATES, evaluate
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from strategy.reference import MovingAverageCrossStrategy

from tests.gate_support import PARITY_WINDOWS, paper_cycle, state_at
from tests.operator_support import ICON_LINK, history_app, sqlite_settings

OPERATOR_TOKEN = "operator-secret-history-4b1e"
ADMIN_TOKEN = "admin-secret-history-93c7"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
BROWSER = {**OPERATOR, "accept": "text/html,application/xhtml+xml"}
LISTS = {
    "orders": "orders",
    "signals": "signals",
    "risk_decisions": "risk decisions",
    "discrepancies": "discrepancies",
    "events": "system events",
}
VIEWS = [*LISTS, "risk"]


@pytest.fixture(autouse=True)
def tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def add(tmp_path: Path, *records) -> None:
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        session.add_all(records)
        session.commit()
    engine.dispose()


def signal_row(at: datetime, *, symbol="BTC-USD", strategy="ma-v1", signal_id=None) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id or uuid4(),
        symbol=symbol,
        strategy_version=strategy,
        side="buy",
        quantity=Decimal("0.25"),
        created_at=at,
    )


def decision_row(
    signal: SignalRecord, at: datetime, *, gate: str | None = None, reason: str = "all gates passed"
) -> RiskDecisionRecord:
    return RiskDecisionRecord(
        approval_id=uuid4(),
        signal_id=signal.signal_id,
        approved=gate is None,
        reason=reason,
        failed_gate=gate,
        correlation_id=uuid4(),
        decided_at=at,
    )


def order_row(
    signal: SignalRecord,
    decision: RiskDecisionRecord,
    at: datetime,
    *,
    status: str = "filled",
    order_id: UUID | None = None,
) -> OrderRecord:
    key = order_id or uuid4()
    return OrderRecord(
        order_id=key,
        signal_id=signal.signal_id,
        client_order_id=key,
        strategy_version=signal.strategy_version,
        risk_approval_id=decision.approval_id,
        symbol=signal.symbol,
        side="buy",
        order_type="market",
        quantity=signal.quantity,
        correlation_id=decision.correlation_id,
        status=status,
        created_at=at,
    )


def lineage(at: datetime, *, status="filled", symbol="BTC-USD", strategy="ma-v1", fills=1):
    """A complete signal -> approved decision -> order -> fills chain."""

    signal = signal_row(at, symbol=symbol, strategy=strategy)
    decision = decision_row(signal, at)
    order = order_row(signal, decision, at, status=status)
    fill_rows = [
        FillRecord(
            fill_id=uuid4(),
            order_id=order.order_id,
            broker_fill_id=f"fill-{order.order_id.hex[:8]}-{index}",
            quantity=Decimal("0.125"),
            price=Decimal("64000.5"),
            fee=Decimal("0.4"),
            occurred_at=at,
        )
        for index in range(fills)
    ]
    return [signal, decision, order, *fill_rows]


async def get(application, path: str, headers=OPERATOR, params=None) -> httpx.Response:
    async with client_for(application) as client:
        return await client.get(path, headers=headers, params=params)


def test_market_activity_returns_linked_buy_sell_markers_and_excludes_watch_only(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    buy = lineage(now - timedelta(minutes=4))
    sell = lineage(now - timedelta(minutes=2), symbol="ETH-USD")
    sell[0].side = "sell"
    sell[2].side = "sell"
    add(tmp_path, *buy, *sell)

    activity = application.state.history.market_activity(
        ("BTC-USD", "ETH-USD", "ADA-USD"),
        now - timedelta(hours=1),
        now + timedelta(minutes=1),
        trading_symbols=("BTC-USD",),
    )

    btc, eth, ada = activity["symbols"]
    assert [row["kind"] for row in btc["rows"]] == ["signal", "order", "fill"]
    assert all(row["side"] == "buy" for row in btc["rows"])
    assert all(row["href"].startswith("/operator/history/orders/") for row in btc["rows"])
    assert eth["total"] == eth["shown"] == 0 and not eth["rows"]
    assert ada["total"] == ada["shown"] == 0 and not ada["rows"]
    assert activity["max_days"] == 31 and activity["max_rows"] == 100


def test_market_activity_reports_the_100_row_cap_and_31_day_boundary(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    rows = []
    for index in range(34):
        rows.extend(lineage(now - timedelta(minutes=index), symbol="BTC-USD"))
    add(tmp_path, *rows)

    activity = application.state.history.market_activity(
        ("BTC-USD",), now - timedelta(days=1), now + timedelta(minutes=1)
    )
    tile = activity["symbols"][0]
    assert tile["total"] == 34 * 3
    assert tile["shown"] == 100 and tile["truncated"]
    wide = application.state.history.market_activity(
        ("BTC-USD",), now - timedelta(days=32), now + timedelta(minutes=1)
    )
    assert wide["status"] == "unavailable"
    assert "31 days" in wide["reason"]


# Bounds


class SpyHistory:
    """Records any read; a refused request must never reach the database."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def read(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"{name} was read for a refused request")

        return read


def unbounded_requests(now: datetime) -> list[list[tuple[str, str]]]:
    return [
        [("limit", "0")],
        [("limit", str(MAX_LIMIT + 1))],
        [("limit", "100000")],
        [("limit", "all")],
        [("limit", "-5")],
        [("window", "all")],
        [("window", "90d")],
        [("since", "1970-01-01T00:00:00Z")],
        [("since", (now - timedelta(days=32)).isoformat())],
        [("since", now.isoformat()), ("until", (now - timedelta(hours=1)).isoformat())],
        [("since", (now - timedelta(hours=1)).isoformat()), ("window", "1h")],
        [("until", "yesterday")],
        [("offset", "0")],
        [("all", "true")],
        [("page", "2")],
        [("limit", "5"), ("limit", "6")],
        [("before", "not-a-cursor")],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", VIEWS)
async def test_every_endpoint_refuses_unbounded_requests_before_reading(tmp_path, kind):
    application = create_app(sqlite_settings(tmp_path))
    spy = SpyHistory()
    application.state.history = spy
    async with client_for(application) as client:
        for params in unbounded_requests(utc_now()):
            response = await client.get(PATHS[kind], headers=OPERATOR, params=params)
            assert response.status_code == 422, (params, response.text)
            detail = response.json()["detail"]
            assert detail["status"] == "refused" and detail["errors"], params
            page = await client.get(PATHS[kind], headers=BROWSER, params=params)
            assert page.status_code == 422, params
            assert "Request refused. Nothing was read." in page.text
    assert spy.calls == []


def test_refusals_name_what_is_wrong():
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    cases = {
        "limit must be a whole number from 1 to 100.": [("limit", "101")],
        "The time window can be at most 31 days.": [("since", "2026-08-01T00:00:00Z")],
        "window must be one of 1h, 24h, 7d, 31d.": [("window", "1y")],
        "Unknown parameter 'offset'.": [("offset", "10")],
        "Give 'symbol' once.": [("symbol", "BTC-USD"), ("symbol", "ETH-USD")],
        "status must be one of pending_submit, unknown, open, partially_filled, filled, "
        "canceled, rejected.": [("status", "done")],
        "client_order_id must be a UUID.": [("client_order_id", "abc")],
        "symbol can be at most 128 characters.": [("symbol", "X" * 129)],
        "since must be earlier than until.": [
            ("since", "2026-09-27T12:00:00Z"),
            ("until", "2026-09-27T11:00:00Z"),
        ],
    }
    for message, params in cases.items():
        with pytest.raises(ValueError) as refused:
            parse_query("orders", params, now=now)
        assert message in refused.value.errors


def test_a_request_without_bounds_gets_the_default_window_and_page():
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    for kind in (*LISTS, "refusals"):
        query = parse_query(kind, [], now=now)
        assert (query.until - query.since, query.limit) == (timedelta(hours=24), DEFAULT_LIMIT)
    widest = parse_query("orders", [("window", "31d"), ("limit", "100")], now=now)
    assert (widest.until - widest.since, widest.limit) == (timedelta(days=31), MAX_LIMIT)
    # Browser date-time fields carry no offset; they are read as UTC, and blanks as absent.
    local = parse_query(
        "orders", [("since", "2026-09-27T08:00"), ("window", ""), ("symbol", "")], now=now
    )
    assert local.since == datetime(2026, 9, 27, 8, tzinfo=UTC) and local.filters == {}


@pytest.mark.asyncio
async def test_page_size_and_window_bound_every_list(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    rows = []
    for minutes in range(30):
        rows += lineage(now - timedelta(minutes=minutes))
    rows += lineage(now - timedelta(days=2))
    rows += lineage(now - timedelta(days=40))
    add(tmp_path, *rows)

    default = (await get(application, PATHS["orders"])).json()
    assert default["query"]["window"] == "24h" and default["query"]["limit"] == DEFAULT_LIMIT
    assert (default["total"], default["count"]) == (30, DEFAULT_LIMIT)
    assert default["next_before"]
    week = (await get(application, PATHS["orders"], params={"window": "7d", "limit": "100"})).json()
    assert (week["total"], week["count"], week["next_before"]) == (31, 31, None)
    # The 40-day-old order is outside every window the server allows.
    widest = (await get(application, PATHS["orders"], params={"window": "31d"})).json()
    assert widest["total"] == 31
    for kind in ("signals", "risk_decisions"):
        page = (await get(application, PATHS[kind], params={"limit": "7"})).json()
        assert (page["total"], page["count"]) == (30, 7)


@pytest.mark.asyncio
async def test_pages_continue_without_gaps_repeats_or_newer_rows(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    rows = []
    # Groups of three share a timestamp, so the cursor must break ties by ID.
    for index in range(23):
        rows += lineage(now - timedelta(minutes=index // 3), fills=0)
    add(tmp_path, *rows)
    expected = [str(item.client_order_id) for item in rows if isinstance(item, OrderRecord)]

    first = (await get(application, PATHS["orders"], params={"limit": "5"})).json()
    add(tmp_path, *lineage(utc_now()))  # recorded after the first page was read
    seen = [row["client_order_id"] for row in first["rows"]]
    cursor, until = first["next_before"], first["query"]["until"]
    while cursor:
        page = (
            await get(
                application,
                PATHS["orders"],
                params={"limit": "5", "until": until, "before": cursor},
            )
        ).json()
        assert page["total"] == 23
        seen += [row["client_order_id"] for row in page["rows"]]
        cursor = page["next_before"]
    assert sorted(seen) == sorted(expected) and len(seen) == len(set(seen)) == 23
    times = [
        row["created_at"]
        for row in (
            await get(application, PATHS["orders"], params={"limit": "100", "until": until})
        ).json()["rows"]
    ]
    assert times == sorted(times, reverse=True)


# Zero recorded versus not available


@pytest.mark.asyncio
async def test_zero_recorded_and_not_available_never_look_alike(tmp_path):
    for name in ("empty", "broken"):
        (tmp_path / name).mkdir()
    empty = history_app(tmp_path / "empty")
    missing = create_app(sqlite_settings(tmp_path))  # the lifespan never attached history
    unreadable = create_app(sqlite_settings(tmp_path / "broken"))
    # A database without the history tables: every read fails.
    no_tables = create_engine(f"sqlite+pysqlite:///{tmp_path / 'broken' / 'none.db'}", future=True)
    unreadable.state.history = SqlAlchemyHistory(sessionmaker(bind=no_tables))
    for kind, noun in LISTS.items():
        recorded = await get(empty, PATHS[kind])
        assert recorded.status_code == 200
        assert recorded.json()["status"] == "available"
        assert (recorded.json()["total"], recorded.json()["rows"]) == (0, [])
        page = (await get(empty, PATHS[kind], headers=BROWSER)).text
        assert f'<span class="count">0</span> {noun} recorded in this window.' in page
        assert "Not available" not in page

        for application, reason in (
            (missing, "the history database is not configured in this process"),
            (unreadable, "the history database could not be read"),
        ):
            response = await get(application, PATHS[kind])
            assert response.status_code == 503
            assert response.json()["detail"] == {"status": "unavailable", "reason": reason}
            html = await get(application, PATHS[kind], headers=BROWSER)
            assert html.status_code == 503
            assert f"Not available: {reason}." in html.text
            assert "this is not a count of zero" in html.text
            assert '<span class="count">0</span>' not in html.text

    risk = (await get(empty, PATHS["risk"], headers=BROWSER)).text
    assert '<span class="count">0</span> refusals recorded in this window.' in risk
    assert '<span class="count">0</span> kill-switch changes recorded in this window.' in risk
    assert (await get(missing, PATHS["risk"])).status_code == 503
    lineage_page = await get(missing, f"{PATHS['orders']}/{uuid4()}", headers=BROWSER)
    assert lineage_page.status_code == 503 and "Not available:" in lineage_page.text


@pytest.mark.asyncio
async def test_a_missing_link_in_a_lineage_says_not_recorded(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    signal = signal_row(now)
    decision = decision_row(signal, now)
    order = order_row(signal, decision, now, status="filled")
    add(tmp_path, decision, order)  # no signal row, and a filled order with no fills

    row = (await get(application, f"{PATHS['orders']}/{order.client_order_id}")).json()["lineage"]
    assert row["signal"] is None and row["risk_decision"]["outcome"] == "approved"
    assert row["gaps"] == ["signal", "fills"]
    html = (
        await get(application, f"{PATHS['orders']}/{order.client_order_id}", headers=BROWSER)
    ).text
    assert "Signal not recorded." in html
    assert "Lineage gap" in html
    assert "The order executed, but no fills are recorded." in html
    assert '<span class="count">0</span> fills recorded.' in html


# Lineage from persisted rows


def quote_for(price: Decimal) -> Quote:
    return Quote(symbol="BTC-USD", bid=price, ask=price, as_of=utc_now(), source="history")


@pytest.mark.asyncio
async def test_lineage_is_rebuilt_from_persisted_rows_after_a_restart(tmp_path):
    settings = sqlite_settings(tmp_path)
    history_app(tmp_path)  # creates every table
    engine, session_factory = database(tmp_path)
    candles = PARITY_WINDOWS["calm_range"]
    broker = SimulatedBroker(quote_for(candles[0].open))
    cycle = paper_cycle(
        broker,
        MovingAverageCrossStrategy(),
        store=SqlAlchemyOrderStore(session_factory),
        audit=SqlAlchemyAuditStore(session_factory),
    )
    outcomes = []
    for index in range(len(candles) - 1):
        broker.set_quote(quote_for(candles[index + 1].open))
        outcomes.append(await cycle.on_market_state(state_at(candles, index)))
    submitted = [item for item in outcomes if item.status is CycleStatus.SUBMITTED]
    assert submitted
    engine.dispose()

    # A new process: nothing in memory, only the database.
    restarted = create_app(settings, recover_on_start=True)
    async with restarted.router.lifespan_context(restarted), client_for(restarted) as client:
        state = (await client.get("/operator/state", headers=OPERATOR)).json()
        assert state["orders"] == [] and state["signals"] == []
        for outcome in submitted:
            key = outcome.order.request.client_order_id
            response = await client.get(f"{PATHS['orders']}/{key}", headers=OPERATOR)
            assert response.status_code == 200
            row = response.json()["lineage"]
            assert row["client_order_id"] == str(key)
            assert row["correlation_id"] == str(outcome.signal.correlation_id)
            assert row["signal"]["signal_id"] == str(outcome.signal.signal_id)
            assert row["signal"]["strategy_version"] == outcome.signal.strategy_version
            decision = row["risk_decision"]
            assert decision["approval_id"] == str(outcome.decision.approval_id)
            assert decision["outcome"] == "approved"
            assert row["status"] == "filled" and row["fill_count"] >= 1
            assert Decimal(row["filled_quantity"]) == outcome.signal.quantity
            assert row["gaps"] == []
            html = (await client.get(f"{PATHS['orders']}/{key}", headers=BROWSER)).text
            assert f'<code class="copy">{key}</code>' in html
            assert f'<code class="copy">{outcome.signal.correlation_id}</code>' in html
            assert "Lineage complete" in html
        listed = await client.get(PATHS["orders"], headers=OPERATOR, params={"limit": "100"})
        assert {row["client_order_id"] for row in listed.json()["rows"]} == {
            str(item.order.request.client_order_id) for item in submitted
        }


@pytest.mark.asyncio
async def test_signals_say_why_an_order_did_or_did_not_happen(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    placed = lineage(now - timedelta(minutes=3))
    refused_signal = signal_row(now - timedelta(minutes=2))
    refused = decision_row(
        refused_signal, now, gate="duplicate_prevention", reason="signal already has an order"
    )
    undecided = signal_row(now - timedelta(minutes=1))
    add(tmp_path, *placed, refused_signal, refused, undecided)

    rows = {
        row["signal_id"]: row for row in (await get(application, PATHS["signals"])).json()["rows"]
    }
    assert rows[str(placed[0].signal_id)]["order"]["status"] == "filled"
    refusal = rows[str(refused_signal.signal_id)]
    assert refusal["order"] is None and refusal["decision"]["gate_position"] == 7
    assert rows[str(undecided.signal_id)]["decision"] is None
    html = (await get(application, PATHS["signals"], headers=BROWSER)).text
    assert "No order: refused at Gate 7 of 17: Duplicate signal or order." in html
    assert "No risk decision is recorded, so no order was placed." in html
    assert f'href="{PATHS["orders"]}/{placed[2].client_order_id}"' in html


# Safety


SECRETS = (
    "sk-live-9f2c7d1e",
    "acct-7781",
    "raw-provider-body",
    "ops-pager@example.test",
    "https://hooks.example.test/private",
    "tk_live_secret_value",
    OPERATOR_TOKEN,
    ADMIN_TOKEN,
)


@pytest.mark.asyncio
async def test_no_provider_payload_or_secret_reaches_any_history_response(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    chain = lineage(now)
    order = chain[2]
    leaky_signal = signal_row(now)
    leaky = decision_row(
        leaky_signal,
        now,
        gate="broker_health",
        reason="broker is unavailable api_key=sk-live-9f2c7d1e see https://hooks.example.test/private",
    )
    add(
        tmp_path,
        *chain,
        leaky_signal,
        leaky,
        DiscrepancyRecord(
            discrepancy_id=uuid4(),
            entity_type="position",
            entity_key="BTC-USD",
            local_payload={
                "quantity": "1",
                "account_id": "acct-7781",
                "api_key": "sk-live-9f2c7d1e",
            },
            broker_payload={"quantity": "2", "raw": {"body": "raw-provider-body"}},
            safety_action="halted",
            created_at=now,
        ),
        DiscrepancyRecord(
            discrepancy_id=uuid4(),
            entity_type="order",
            entity_key=str(order.client_order_id),
            local_payload={"value": "('open', '0')"},
            broker_payload={},
            safety_action="halted",
            created_at=now,
        ),
        SystemEventRecord(
            event_id=uuid4(),
            event_type="provider_callback",
            correlation_id=uuid4(),
            payload={"body": "raw-provider-body", "token": "tk_live_secret_value"},
            created_at=now,
        ),
        SystemEventRecord(
            event_id=uuid4(),
            event_type="kill_switch_transition",
            correlation_id=uuid4(),
            payload={
                "from": "halted",
                "to": "running",
                "actor": "admin",
                "automatic": False,
                "reason": "INC-9 cleared by ops-pager@example.test token=tk_live_secret_value",
                "checklist": ["cause_documented", "raw-provider-body"],
                "account_id": "acct-7781",
            },
            created_at=now,
        ),
    )
    paths = [PATHS[kind] for kind in VIEWS] + [f"{PATHS['orders']}/{order.client_order_id}"]
    for path in paths:
        for headers in (OPERATOR, ADMIN, BROWSER, {**ADMIN, "accept": "text/html"}):
            response = await get(application, path, headers=headers, params=None)
            assert response.status_code == 200, (path, response.text)
            for secret in SECRETS:
                assert secret not in response.text, (path, secret)
    discrepancies = (await get(application, PATHS["discrepancies"])).json()["rows"]
    position = next(row for row in discrepancies if row["entity_type"] == "position")
    assert position["differs"] == ["quantity"]
    assert "local_payload" not in position and "broker_payload" not in position
    events = (await get(application, PATHS["events"])).json()["rows"]
    assert next(row for row in events if row["event_type"] == "provider_callback")["detail"] is None
    change = next(row for row in events if row["detail"])["detail"]
    assert change["checklist"] == ["cause_documented"]
    assert "[REDACTED]" in change["reason"]


@pytest.mark.asyncio
async def test_history_routes_only_read(tmp_path):
    application = history_app(tmp_path)
    for route in history_router.routes:
        assert route.methods <= {"GET", "HEAD"}, route.path
    order_path = f"{PATHS['orders']}/{uuid4()}"
    async with client_for(application) as client:
        for path in [*(PATHS[kind] for kind in VIEWS), order_path]:
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(method, path, headers=ADMIN)
                assert response.status_code == 405, (method, path)
    # The read model holds a session factory and nothing that can reach a broker.
    assert set(vars(application.state.history)) == {"session_factory"}


@pytest.mark.asyncio
async def test_history_requires_an_operator_session_and_never_a_url_token(tmp_path):
    application = history_app(tmp_path)
    async with client_for(application) as client:
        for kind in VIEWS:
            assert (await client.get(PATHS[kind])).status_code == 401
            refused = await client.get(PATHS[kind], params={"token": OPERATOR_TOKEN})
            assert refused.status_code == 400
        await client.post(
            "/operator/login",
            data={"token": OPERATOR_TOKEN},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        page = await client.get(PATHS["orders"], headers={"accept": "text/html"})
        assert page.status_code == 200 and "<dd>Operator</dd>" in page.text


# Risk and safety history


def test_the_gate_list_matches_the_risk_engine_order():
    source = inspect.getsource(evaluate)
    gates = list(dict.fromkeys(re.findall(r'_reject\(\s*signal,\s*"(\w+)"', source)))
    assert gates == [gate for gate, _ in RISK_GATES]
    assert len(RISK_GATES) == 17


@pytest.mark.asyncio
async def test_refusals_are_grouped_by_the_seventeen_ordered_gates(tmp_path):
    application = history_app(tmp_path)
    application.state.kill_switch.trip("scheduled reconciliation diverged")
    now = utc_now()
    rows = []
    for minutes, gate in [
        (50, "kill_switch"),
        (40, "cash_reserve"),
        (5, "cash_reserve"),
        (30, "risk_inputs"),
        (20, "legacy_gate"),
    ]:
        signal = signal_row(now - timedelta(minutes=minutes))
        rows += [signal, decision_row(signal, now - timedelta(minutes=minutes), gate=gate)]
    newest = signal_row(now - timedelta(minutes=1))
    rows += [newest, decision_row(newest, now - timedelta(minutes=1), gate="exchange_constraints")]
    rows += lineage(now - timedelta(minutes=2))  # one approval
    old = signal_row(now - timedelta(days=3))
    rows += [old, decision_row(old, now - timedelta(days=3), gate="drawdown")]
    add(tmp_path, *rows)

    payload = (await get(application, PATHS["risk"])).json()
    refusals = payload["refusals"]
    assert [gate["gate"] for gate in refusals["gates"]] == [gate for gate, _ in RISK_GATES]
    assert [gate["position"] for gate in refusals["gates"]] == list(range(1, 18))
    counts = {gate["gate"]: gate["refusals"] for gate in refusals["gates"] + refusals["other"]}
    assert counts["cash_reserve"] == 2 and counts["kill_switch"] == 1
    assert counts["exchange_constraints"] == 1 and counts["drawdown"] == 0  # outside the window
    assert counts["risk_inputs"] == 1 and counts["legacy_gate"] == 1
    assert (refusals["decisions"], refusals["approved"], refusals["refused"]) == (7, 1, 6)
    assert refusals["latest_refusal"]["failed_gate"] == "exchange_constraints"
    assert refusals["latest_refusal"]["signal"]["signal_id"] == str(newest.signal_id)
    kill_switch = payload["kill_switch"]
    assert kill_switch["state"] == "halted" and kill_switch["total"] == 1
    assert kill_switch["rows"][0]["detail"]["to"] == "halted"

    decisions = (await get(application, PATHS["risk_decisions"], params={"limit": "100"})).json()
    legacy = next(row for row in decisions["rows"] if row["failed_gate"] == "legacy_gate")
    assert (legacy["gate_label"], legacy["gate_position"]) == ("Unrecognised gate", None)
    inputs = next(row for row in decisions["rows"] if row["failed_gate"] == "risk_inputs")
    assert inputs["gate_label"] == "Risk inputs could not be assembled"

    week = (await get(application, PATHS["risk"], params={"window": "7d"})).json()
    assert {gate["gate"]: gate["refusals"] for gate in week["refusals"]["gates"]}["drawdown"] == 1

    html = (await get(application, PATHS["risk"], headers=BROWSER)).text
    positions = [html.index(f'<span class="mono age">{gate}</span>') for gate, _ in RISK_GATES]
    assert positions == sorted(positions)
    assert "Unrecognised gate" in html and "Risk inputs could not be assembled" in html
    listed = (await get(application, PATHS["risk_decisions"], headers=BROWSER)).text
    assert "Unrecognised gate (legacy_gate)." in listed
    assert "Before the gates: Risk inputs could not be assembled." in listed
    assert "Gate 17 of 17: Exchange quantity, price, and notional constraints." in html
    assert "none in this window" in html
    assert "running to halted" in html and "The system (automatic)" in html


# Filters


@pytest.mark.asyncio
async def test_filters_narrow_each_view(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    btc = lineage(now - timedelta(minutes=1), symbol="BTC-USD", strategy="ma-v1")
    eth = lineage(now - timedelta(minutes=2), symbol="ETH-USD", strategy="ma-v2", status="unknown")
    refused_signal = signal_row(now, symbol="ETH-USD", strategy="ma-v2")
    refused = decision_row(refused_signal, now, gate="stale_price", reason="quote is stale")
    add(
        tmp_path,
        *btc,
        *eth,
        refused_signal,
        refused,
        DiscrepancyRecord(
            discrepancy_id=uuid4(),
            entity_type="balance",
            entity_key="USD",
            local_payload={"available": "10"},
            broker_payload={"available": "9"},
            safety_action="halted",
            created_at=now,
        ),
    )
    application.state.kill_switch.trip("drill")

    async def ids(kind, key, **params):
        rows = (await get(application, PATHS[kind], params=params)).json()["rows"]
        return [row[key] for row in rows]

    eth_order = str(eth[2].client_order_id)
    assert await ids("orders", "client_order_id", symbol="eth-usd") == [eth_order]
    assert await ids("orders", "client_order_id", status="unknown") == [eth_order]
    assert await ids("orders", "client_order_id", strategy_version="ma-v1") == [
        str(btc[2].client_order_id)
    ]
    assert await ids("orders", "client_order_id", client_order_id=eth_order) == [eth_order]
    assert await ids("orders", "client_order_id", correlation_id=str(eth[2].correlation_id)) == [
        eth_order
    ]
    assert await ids("orders", "client_order_id", status="open") == []
    assert await ids("signals", "signal_id", strategy_version="ma-v2", symbol="ETH-USD") == [
        str(refused_signal.signal_id),
        str(eth[0].signal_id),
    ]
    assert await ids("risk_decisions", "approval_id", outcome="refused") == [
        str(refused.approval_id)
    ]
    assert await ids("risk_decisions", "approval_id", failed_gate="stale_price") == [
        str(refused.approval_id)
    ]
    assert await ids("risk_decisions", "approval_id", outcome="approved", symbol="BTC-USD") == [
        str(btc[1].approval_id)
    ]
    assert await ids(
        "risk_decisions", "approval_id", correlation_id=str(refused.correlation_id)
    ) == [str(refused.approval_id)]
    assert await ids("discrepancies", "entity_key", entity_type="balance") == ["USD"]
    assert await ids("discrepancies", "entity_key", entity_type="order") == []
    assert len(await ids("events", "event_id", event_type="kill_switch_transition")) == 1

    # The page keeps what was asked and offers the next step without a token.
    html = (
        await get(
            application,
            PATHS["risk_decisions"],
            headers=BROWSER,
            params={"failed_gate": "stale_price", "symbol": "ETH-USD"},
        )
    ).text
    assert '<option value="stale_price" selected>4. Stale or future quote</option>' in html
    assert 'name="symbol" type="text" value="ETH-USD"' in html


# Order statuses and page structure


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings: list[int] = []
        self.forms: list[dict] = []
        self.urls: list[str] = []
        self.visible_marks = 0
        self.stack: list[bool] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag == "form":
            self.forms.append(attributes)
        for name in ("href", "action"):
            if attributes.get(name):
                self.urls.append(attributes[name])
        if tag in {"meta", "input", "br"}:
            return
        hidden = attributes.get("aria-hidden") == "true"
        self.stack.append(hidden)
        self.hidden += hidden

    def handle_endtag(self, tag):
        if tag in {"meta", "input", "br"} or not self.stack:
            return
        self.hidden -= self.stack.pop()

    def handle_data(self, data):
        if self.hidden == 0 and self.lasttag not in {"style", "title"}:
            self.visible_marks += sum(data.count(mark) for mark in "●▲■○◐◆")


@pytest.mark.asyncio
async def test_every_order_status_is_shown_and_unknown_carries_the_rule(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    rows = []
    for index, status in enumerate(OrderStatus):
        rows += lineage(now - timedelta(minutes=index), status=status.value, fills=0)
    add(tmp_path, *rows)

    payload = (await get(application, PATHS["orders"])).json()
    unknown = next(row for row in payload["rows"] if row["status"] == "unknown")
    assert unknown["rule"] == "Look it up by client order ID. Never resubmit it."
    assert all("rule" not in row for row in payload["rows"] if row["status"] != "unknown")

    html = (await get(application, PATHS["orders"], headers=BROWSER)).text
    tones = {
        "pending_submit": ("warn", "▲"),
        "unknown": ("crit", "■"),
        "open": ("neutral", "◐"),
        "partially_filled": ("neutral", "◐"),
        "filled": ("ok", "●"),
        "canceled": ("neutral", "◐"),
        "rejected": ("crit", "■"),
    }
    ledger = html[html.index('<ol class="ledger">') : html.index('id="statuses-heading"')]
    legend = html[html.index('id="statuses-heading"') :]
    for status, (tone, mark) in tones.items():
        pill = (
            f'<span class="pill tone-{tone}"><span class="mark" aria-hidden="true">{mark}</span> '
            f"{status}</span>"
        )
        assert pill in ledger and pill in legend, status
    summary = re.search(
        r"<summary>(?:(?!</summary>).)*■</span> unknown</span>.*?</summary>", html, re.S
    )
    assert summary and "Never resubmit it." in summary.group(0)
    assert "The submission outcome is ambiguous. Look it up by client order ID." in legend


@pytest.mark.asyncio
async def test_history_pages_are_structured_script_free_and_token_free(tmp_path):
    application = history_app(tmp_path)
    now = utc_now()
    chain = lineage(now)
    add(tmp_path, *chain, *lineage(now - timedelta(minutes=1)))
    paths = [PATHS[kind] for kind in VIEWS] + [f"{PATHS['orders']}/{chain[2].client_order_id}"]
    for path in paths:
        html = (await get(application, path, headers=BROWSER, params=None)).text
        parsed = _Page()
        parsed.feed(html)
        assert parsed.headings.count(1) == 1 and parsed.headings[0] == 1, path
        assert all(b - a <= 1 for a, b in zip(parsed.headings, parsed.headings[1:], strict=False))
        assert parsed.visible_marks == 0, path
        assert "<script" not in html
        assert html.count("<link") == 1 and ICON_LINK in html, path
        assert '<a class="skip-link" href="#main">Skip to main content</a>' in html
        assert '<nav class="history-nav" aria-label="History views">' in html
        nav = html[html.index('<nav class="history-nav"') : html.index("</nav>")]
        assert nav.count('aria-current="page"') == (0 if "/orders/" in path else 1)
        methods = [(form.get("method"), form.get("action")) for form in parsed.forms]
        assert ("post", "/operator/logout") in methods
        assert all(method == "get" for method, action in methods if action != "/operator/logout")
        for url in parsed.urls:
            assert "token" not in url.lower(), url

    paged = (await get(application, PATHS["orders"], headers=BROWSER, params={"limit": "1"})).text
    older = re.search(r'href="(/operator/history/orders\?[^"]+)">Older orders</a>', paged)
    assert older and "before=" in older.group(1) and "until=" in older.group(1)
    next_page = (await get(application, older.group(1).replace("&amp;", "&"), headers=BROWSER)).text
    assert ">Newest</a>" in next_page and "continuing from an earlier page" in next_page


@pytest.mark.asyncio
async def test_lineage_lookup_refuses_bad_ids_and_reports_unknown_orders(tmp_path):
    application = history_app(tmp_path)
    missing = uuid4()
    async with client_for(application) as client:
        bad = await client.get(f"{PATHS['orders']}/not-a-uuid", headers=OPERATOR)
        assert bad.status_code == 422 and bad.json()["detail"]["status"] == "refused"
        extra = await client.get(f"{PATHS['orders']}/{missing}?limit=5", headers=OPERATOR)
        assert extra.status_code == 422
        unknown = await client.get(f"{PATHS['orders']}/{missing}", headers=OPERATOR)
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == {
            "status": "not_recorded",
            "reason": "No order with this client order ID.",
        }
        page = await client.get(f"{PATHS['orders']}/{missing}", headers=BROWSER)
        assert page.status_code == 404
        assert "No order with this client order ID." in page.text
        assert f'<code class="copy">{missing}</code>' in page.text


@pytest.mark.asyncio
async def test_dashboard_links_to_the_history_pages(tmp_path):
    html = (await get(history_app(tmp_path), "/operator", headers=BROWSER)).text
    assert '<a href="/operator/history/orders">Order history</a>' in html
    assert '<a href="/operator/history/risk-decisions">History</a>' in html
    assert '<a href="/operator/history/risk">Risk &amp; safety history</a>' in html


# Indexes


def test_every_history_read_searches_an_index(tmp_path):
    history_app(tmp_path)
    now = utc_now()
    chain = lineage(now)
    add(
        tmp_path,
        *chain,
        DiscrepancyRecord(
            discrepancy_id=uuid4(),
            entity_type="order",
            entity_key="x",
            local_payload={},
            broker_payload={},
            safety_action="halted",
            created_at=now,
        ),
        SystemEventRecord(
            event_id=uuid4(),
            event_type="kill_switch_transition",
            correlation_id=uuid4(),
            payload={},
            created_at=now,
        ),
    )
    engine, session_factory = database(tmp_path)
    statements: list[tuple[str, tuple]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    history = SqlAlchemyHistory(session_factory)
    order = chain[2]
    history.orders(parse_query("orders", []))
    history.orders(parse_query("orders", [("correlation_id", str(order.correlation_id))]))
    history.signals(parse_query("signals", []))
    history.risk_decisions(parse_query("risk_decisions", []))
    history.discrepancies(parse_query("discrepancies", []))
    history.events(parse_query("events", [("event_type", "kill_switch_transition")]))
    history.refusals(parse_query("refusals", []))
    history.lineage(order.client_order_id)
    event.remove(engine, "before_cursor_execute", capture)

    tables = "signals|risk_decisions|orders|fills|discrepancies|system_events"
    with engine.connect() as connection:
        for statement, parameters in statements:
            plan = [
                row[-1]
                for row in connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                ).all()
            ]
            assert any(re.search(rf"SEARCH ({tables}) USING", step) for step in plan), plan
            assert not any(re.fullmatch(rf"SCAN ({tables})", step) for step in plan), plan
    assert len(statements) >= 20
    engine.dispose()
