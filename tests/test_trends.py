"""Trends: bounded, counted by the database, correct at the edges, and truthful about absence."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import api.trends as trends_module
import httpx
import pytest
from api.history import HistoryQueryRefused
from api.history_routes import router as history_router
from api.history_view import PATHS
from api.trends import (
    MAX_BUCKETS,
    MAX_WINDOW,
    WINDOWS,
    SqlAlchemyTrends,
    parse_trends_query,
    trends_query,
)
from api.trends_view import _scale
from app.main import create_app
from db.models import (
    Base,
    DiscrepancyRecord,
    EquitySnapshotRecord,
    PortfolioSnapshotRecord,
    RiskDecisionRecord,
)
from risk.engine import RISK_GATES
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from tests.operator_support import ICON_LINK, history_app, sqlite_settings

OPERATOR_TOKEN = "operator-secret-trends-5d2a"
ADMIN_TOKEN = "admin-secret-trends-81fe"
OPERATOR = {"x-operator-token": OPERATOR_TOKEN}
ADMIN = {"x-operator-token": ADMIN_TOKEN}
BROWSER = {**OPERATOR, "accept": "text/html,application/xhtml+xml"}
PATH = PATHS["trends"]
# Mid-hour, so no bucket edge falls on the clock during a test.
NOW = datetime(2026, 9, 27, 14, 23, 45, tzinfo=UTC)
TICK = timedelta(microseconds=1)
DATA = ("reconciliation_runs", "discrepancies", "refusals")
NOT_STARTED = ("uptime_freshness", "equity_pnl")


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))
    # The routes read the clock through the trends module; pin it.
    monkeypatch.setattr(trends_module, "utc_now", lambda: NOW)


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


async def get(application, path=PATH, *, headers=OPERATOR, params=None) -> httpx.Response:
    async with client_for(application) as client:
        return await client.get(path, headers=headers, params=params)


def database(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'trader.db'}", future=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def add(tmp_path: Path, *records) -> None:
    engine, session_factory = database(tmp_path)
    with session_factory() as session:
        session.add_all(records)
        session.commit()
    engine.dispose()


def reader(tmp_path: Path) -> SqlAlchemyTrends:
    engine, session_factory = database(tmp_path)
    Base.metadata.create_all(engine)
    return SqlAlchemyTrends(session_factory)


def run(at: datetime, source: str = "broker") -> PortfolioSnapshotRecord:
    return PortfolioSnapshotRecord(batch_id=uuid4(), source=source, recorded_at=at)


def discrepancy(at: datetime, kind: str = "order", **payloads) -> DiscrepancyRecord:
    return DiscrepancyRecord(
        discrepancy_id=uuid4(),
        entity_type=kind,
        entity_key=str(uuid4()),
        local_payload=payloads.get("local", {"status": "open"}),
        broker_payload=payloads.get("broker", {"status": "filled"}),
        safety_action="halted",
        created_at=at,
    )


def decision(at: datetime, gate: str | None, *, approved: bool = False) -> RiskDecisionRecord:
    return RiskDecisionRecord(
        approval_id=uuid4(),
        signal_id=uuid4(),
        approved=approved,
        reason="all gates passed" if approved else f"refused at {gate}",
        failed_gate=gate,
        correlation_id=uuid4(),
        decided_at=at,
    )


def charts_by_key(charts) -> dict:
    return {chart["key"]: chart for chart in charts}


def series(chart, key) -> dict:
    return next(row for row in chart["series"] if row["key"] == key)


def section(html: str, key: str) -> str:
    anchor = f'id="chart-{key.replace("_", "-")}"'
    start = html.index(anchor)
    return html[start : html.index("</section>", start)]


# Windows and their edges


def test_buckets_align_to_the_clock_and_end_with_the_one_now_running():
    expected = {
        "24h": (datetime(2026, 9, 26, 15, tzinfo=UTC), datetime(2026, 9, 27, 15, tzinfo=UTC), 24),
        "7d": (datetime(2026, 9, 20, 18, tzinfo=UTC), datetime(2026, 9, 27, 18, tzinfo=UTC), 28),
        "30d": (datetime(2026, 8, 29, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC), 30),
    }
    for window, (since, until, buckets) in expected.items():
        query = trends_query(window, NOW)
        assert (query.since, query.until, query.buckets) == (since, until, buckets), window
        assert query.edges[-2] <= NOW < query.until  # the last bucket is the one running now
        steps = {
            later - earlier for earlier, later in zip(query.edges, query.edges[1:], strict=False)
        }
        assert steps == {WINDOWS[window][1]}
        # The same instant in another zone gives the same UTC buckets.
        eastern = NOW.astimezone(datetime.fromisoformat("2026-01-01T00:00:00-05:00").tzinfo)
        assert trends_query(window, eastern).edges == query.edges
    # On an edge, the new bucket has begun.
    on_edge = trends_query("24h", datetime(2026, 9, 27, 15, tzinfo=UTC))
    assert on_edge.edges[-2] == datetime(2026, 9, 27, 15, tzinfo=UTC)


def test_the_window_is_capped_on_the_server(monkeypatch):
    for window in WINDOWS:
        query = parse_trends_query([("window", window)], now=NOW)
        assert query.until - query.since <= MAX_WINDOW == timedelta(days=30)
        assert query.buckets <= MAX_BUCKETS == 30
    default = parse_trends_query([], now=NOW)
    assert default.window == "24h" and parse_trends_query([("window", "")], now=NOW) == default
    cases = {
        "window must be one of 24h, 7d, 30d.": [("window", "31d")],
        "Give 'window' once.": [("window", "24h"), ("window", "7d")],
        "Unknown parameter 'since'.": [("since", "2026-01-01T00:00:00Z")],
    }
    for message, params in cases.items():
        with pytest.raises(HistoryQueryRefused) as refused:
            parse_trends_query(params, now=NOW)
        assert message in refused.value.errors
    # Even a window the table offered would be refused past 30 days.
    monkeypatch.setitem(WINDOWS, "90d", (timedelta(days=90), timedelta(days=3)))
    with pytest.raises(HistoryQueryRefused, match="at most 30 days"):
        parse_trends_query([("window", "90d")], now=NOW)


class SpyTrends:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def read(self, query):
        self.calls.append(query)
        raise AssertionError("a refused request must not read")


@pytest.mark.asyncio
async def test_a_wider_or_malformed_request_is_refused_before_reading(tmp_path):
    application = create_app(sqlite_settings(tmp_path))
    spy = SpyTrends()
    application.state.trends = spy
    requests = [
        [("window", "31d")],
        [("window", "90d")],
        [("window", "1y")],
        [("window", "all")],
        [("window", "24h"), ("window", "30d")],
        [("since", "2026-01-01T00:00:00Z")],
        [("until", "2026-09-27T00:00:00Z")],
        [("limit", "1000")],
        [("bucket", "1m")],
    ]
    async with client_for(application) as client:
        for params in requests:
            response = await client.get(PATH, headers=OPERATOR, params=params)
            assert response.status_code == 422, params
            assert response.json()["detail"]["status"] == "refused"
            page = await client.get(PATH, headers=BROWSER, params=params)
            assert page.status_code == 422, params
            assert "Request refused. Nothing was read." in page.text
            assert "<svg" not in page.text
    assert spy.calls == []


# Counting


def test_counts_are_exact_at_every_window_edge(tmp_path):
    for window in WINDOWS:
        folder = tmp_path / window
        folder.mkdir()
        trends = reader(folder)
        query = trends_query(window, NOW)
        edges, middle = query.edges, query.buckets // 2
        inside = [edges[0], edges[middle] - TICK, edges[middle], edges[-1] - TICK]
        outside = [edges[0] - TICK, edges[-1]]
        add(
            folder,
            *(run(at) for at in inside + outside),
            run(edges[1], source="local"),  # not a broker snapshot, so not a run
            *(discrepancy(at, "balance") for at in inside + outside),
            *(decision(at, "stale_price") for at in inside + outside),
            decision(edges[1], None, approved=True),  # approvals are not refusals
        )
        charts = charts_by_key(trends.read(query))
        expected = [0] * query.buckets
        for index in (0, middle - 1, middle, query.buckets - 1):
            expected[index] += 1
        assert series(charts["reconciliation_runs"], "completed")["counts"] == expected, window
        assert series(charts["discrepancies"], "balance")["counts"] == expected, window
        assert series(charts["refusals"], "stale_price")["counts"] == expected, window
        for key in DATA:
            chart = charts[key]
            assert chart["total"] == 4 and chart["totals"] == expected, (window, key)
            assert len(chart["buckets"]) == query.buckets
            assert chart["buckets"][0]["start"] == edges[0].isoformat()
            assert chart["buckets"][-1] == {
                "start": edges[-2].isoformat(),
                "end": edges[-1].isoformat(),
                "in_progress": True,
            }


def test_an_empty_window_counts_zero_in_every_bucket_and_series(tmp_path):
    query = trends_query("7d", NOW)
    charts = charts_by_key(reader(tmp_path).read(query))
    assert [chart["key"] for chart in charts.values()] == [*DATA, *NOT_STARTED]
    for key in DATA:
        chart = charts[key]
        assert chart["status"] == "available" and chart["total"] == 0
        assert chart["totals"] == [0] * 28
        assert all(row["counts"] == [0] * 28 for row in chart["series"])
    kinds = [row["key"] for row in charts["discrepancies"]["series"]]
    assert kinds == ["order", "fill", "position", "balance"]
    gates = [row["key"] for row in charts["refusals"]["series"]]
    assert gates == [gate for gate, _ in RISK_GATES] + ["risk_inputs"]


def test_refusals_keep_the_seventeen_gates_in_order_and_every_unknown_gate(tmp_path):
    trends = reader(tmp_path)
    query = trends_query("24h", NOW)
    at = query.edges[3]
    add(
        tmp_path,
        *(decision(at, "daily_loss") for _ in range(3)),
        decision(at, "kill_switch"),
        decision(at, "risk_inputs"),
        decision(at, "retired_gate"),
        decision(at, None),
        *(decision(at, None, approved=True) for _ in range(5)),
    )
    chart = charts_by_key(trends.read(query))["refusals"]
    assert chart["total"] == 7
    rows = chart["series"]
    assert [row["position"] for row in rows[:17]] == list(range(1, 18))
    assert [(row["key"], row["label"]) for row in rows[:17]] == list(RISK_GATES)
    assert rows[17]["label"] == "Before the gates: Risk inputs could not be assembled"
    assert [(row["key"], row["label"], row["recognised"]) for row in rows[18:]] == [
        (None, "No gate recorded", False),
        ("retired_gate", "Unrecognised gate", False),
    ]
    totals = {row["key"]: row["total"] for row in rows if row["total"]}
    assert totals == {
        "kill_switch": 1,
        "daily_loss": 3,
        "risk_inputs": 1,
        None: 1,
        "retired_gate": 1,
    }


def test_discrepancies_are_split_by_type_and_keep_unknown_types(tmp_path):
    trends = reader(tmp_path)
    query = trends_query("30d", NOW)
    add(
        tmp_path,
        discrepancy(query.edges[0], "order"),
        discrepancy(query.edges[0], "order"),
        discrepancy(query.edges[10], "position"),
        discrepancy(query.edges[29], "ledger"),
    )
    chart = charts_by_key(trends.read(query))["discrepancies"]
    assert {row["key"]: row["total"] for row in chart["series"]} == {
        "order": 2,
        "fill": 0,
        "position": 1,
        "balance": 0,
        "ledger": 1,
    }
    assert series(chart, "ledger")["label"] == "Unrecognised type"
    assert chart["totals"][0] == 2 and chart["totals"][10] == 1 and chart["totals"][29] == 1


# Zero, unavailable, and not started


@pytest.mark.asyncio
async def test_zero_unavailable_and_not_started_never_look_alike(tmp_path):
    for name in ("empty", "broken"):
        (tmp_path / name).mkdir()
    empty = history_app(tmp_path / "empty")
    missing = create_app(sqlite_settings(tmp_path))  # the lifespan never attached trends
    unreadable = create_app(sqlite_settings(tmp_path / "broken"))
    no_tables = create_engine(f"sqlite+pysqlite:///{tmp_path / 'broken' / 'none.db'}", future=True)
    unreadable.state.trends = SqlAlchemyTrends(sessionmaker(bind=no_tables))

    payload = (await get(empty)).json()
    assert payload["status"] == "available"
    charts = charts_by_key(payload["charts"])
    assert all(charts[key]["status"] == "available" and charts[key]["total"] == 0 for key in DATA)
    assert all(
        charts[key]["status"] == "not_started" and charts[key]["reason"] for key in NOT_STARTED
    )

    html = (await get(empty, headers=BROWSER)).text
    for key, unit in zip(DATA, ("completed runs", "discrepancies", "refusals"), strict=True):
        zero = section(html, key)
        assert f'<span class="count">0</span> {unit} recorded in this window.' in zero
        assert "<svg" not in zero and "Not available" not in zero and "Not started" not in zero
    for key in NOT_STARTED:
        idle = section(html, key)
        assert "Not started: no producer records this yet." in idle
        assert "no empty axis can be read as zero" in idle
        assert "<svg" not in idle and 'class="count"' not in idle and "Not available" not in idle

    for application, reason in (
        (missing, "the history database is not configured in this process"),
        (unreadable, "the history database could not be read"),
    ):
        response = await get(application)
        assert response.status_code == 503
        assert response.json()["detail"] == {"status": "unavailable", "reason": reason}
        page = await get(application, headers=BROWSER)
        assert page.status_code == 503
        for key in DATA:
            gone = section(page.text, key)
            assert f"Not available: {reason}." in gone
            assert "this is not a count of zero" in gone
            assert "<svg" not in gone and '<span class="count">0</span>' not in gone
        # Not started needs no read, so it still says why.
        for key in NOT_STARTED:
            assert "Not started: no producer records this yet." in section(page.text, key)
    no_tables.dispose()


@pytest.mark.asyncio
async def test_equity_rows_are_never_charted_without_a_trustworthy_producer(tmp_path):
    application = history_app(tmp_path)
    add(
        tmp_path,
        EquitySnapshotRecord(
            snapshot_id=uuid4(), equity=Decimal("98765.4321"), as_of=NOW, source="broker"
        ),
    )
    response = await get(application)
    equity = charts_by_key(response.json()["charts"])["equity_pnl"]
    assert equity["status"] == "not_started" and equity["blocked_by"] == ["#30"]
    assert "series" not in equity and "total" not in equity
    html = (await get(application, headers=BROWSER)).text
    for text in (response.text, html):
        assert "98765" not in text
    assert "(#30)" in section(html, "equity_pnl")


# Rendering


class _Tables(HTMLParser):
    """Every table's caption and cell text, row by row, and every SVG's attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict] = []
        self.svgs: list[dict] = []
        self.headings: list[int] = []
        self.visible_marks = 0
        self._cell: list[str] | None = None
        self._part = ""
        self._hidden: list[bool] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag == "svg":
            self.svgs.append(attributes)
        if tag == "table":
            self.tables.append({"caption": "", "head": [], "body": [], "foot": []})
        elif tag in {"thead", "tbody", "tfoot"}:
            self._part = {"thead": "head", "tbody": "body", "tfoot": "foot"}[tag]
        elif tag == "tr" and self.tables:
            self.tables[-1][self._part].append([])
        elif tag in {"th", "td"} and self.tables:
            self._cell = []
            self.tables[-1][self._part][-1].append((tag, attributes.get("scope"), self._cell))
        elif tag == "caption":
            self._cell = []
            self.tables[-1]["caption"] = self._cell
        if tag not in {"meta", "input", "br", "rect", "line"}:
            self._hidden.append(attributes.get("aria-hidden") == "true")

    def handle_endtag(self, tag):
        if tag in {"th", "td", "caption"}:
            self._cell = None
        if tag not in {"meta", "input", "br", "rect", "line"} and self._hidden:
            self._hidden.pop()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if not any(self._hidden) and self.lasttag not in {"style", "title"}:
            self.visible_marks += sum(data.count(mark) for mark in "●▲■○◐◆")


def _text(parts) -> str:
    return " ".join("".join(parts).split())


def seeded(tmp_path: Path, query):
    edges = query.edges
    add(
        tmp_path,
        *(
            run(edges[index] + timedelta(minutes=5 * step))
            for index in (0, 5, 23)
            for step in (0, 1)
        ),
        run(edges[23] + timedelta(minutes=10)),
        discrepancy(edges[2], "order"),
        discrepancy(edges[2] + timedelta(minutes=30), "balance"),
        discrepancy(edges[3], "order"),  # on the next edge: the next hour's, never both
        decision(edges[7], "stale_price"),
        decision(edges[7], "daily_loss"),
        decision(edges[8], "daily_loss"),
    )


@pytest.mark.asyncio
async def test_every_drawn_chart_has_an_accessible_table_with_the_same_counts(tmp_path):
    application = history_app(tmp_path)
    query = trends_query("24h", NOW)
    seeded(tmp_path, query)
    charts = charts_by_key((await get(application)).json()["charts"])
    html = (await get(application, headers=BROWSER)).text
    for key in DATA:
        chart = charts[key]
        part = _Tables()
        part.feed(section(html, key))
        assert part.svgs, key
        for svg in part.svgs:
            assert svg["role"] == "img" and svg["focusable"] == "false"
            assert svg["aria-label"].strip(), key
        (table,) = part.tables
        assert _text(table["caption"]).startswith(chart["title"])
        assert all(scope == "col" for _, scope, _ in table["head"][0])
        drawn = [row for row in chart["series"] if row["total"]]
        columns = [_text(cell) for _, _, cell in table["head"][0][1:]]
        assert len(columns) == len(drawn) + (1 if len(drawn) > 1 else 0)
        assert len(table["body"]) == query.buckets
        for index, row in enumerate(table["body"]):
            assert row[0][:2] == ("th", "row")
            counts = [int(_text(cell)) for _, _, cell in row[1:]]
            expected = [item["counts"][index] for item in drawn]
            if len(drawn) > 1:
                expected.append(chart["totals"][index])
            assert counts == expected, (key, index)
        assert "in progress" in _text(table["body"][-1][0][2])
        foot = [int(_text(cell)) for _, _, cell in table["foot"][0][1:]]
        assert foot[-1] == chart["total"]
    runs = section(html, "reconciliation_runs")
    assert '<span class="count">7</span> completed runs in this window' in runs
    label = re.search(r'aria-label="([^"]+)"', runs).group(1)
    assert "7 in total" in label and "the most in one hour is 3" in label
    refusals = section(html, "refusals")
    assert "4. Stale or future quote" in refusals and "14. Daily loss" in refusals
    assert "0</span> refusals at the other 16 gates" in refusals


@pytest.mark.asyncio
async def test_a_table_period_links_to_exactly_its_records(tmp_path):
    application = history_app(tmp_path)
    query = trends_query("24h", NOW)
    seeded(tmp_path, query)
    html = (await get(application, headers=BROWSER)).text
    for key, path, expected in (
        ("discrepancies", PATHS["discrepancies"], {2: 2, 3: 1}),
        ("refusals", PATHS["risk_decisions"], {7: 2, 8: 1}),
    ):
        part = section(html, key)
        body = part[part.index("<tbody>") : part.index("</tbody>")]
        links = re.findall(rf'<a href="({re.escape(path)}\?[^"]+)">', body)
        assert len(links) == query.buckets, key  # one per period
        for index, count in expected.items():
            href = links[index].replace("&amp;", "&")
            params = parse_qs(urlsplit(href).query)
            assert params["since"] == [query.edges[index].isoformat()]
            listed = (await get(application, href)).json()
            assert listed["total"] == count, (key, index)
        detail = re.search(r'<a href="([^"]+)">Every one of these', section(html, key))
        whole = (await get(application, detail.group(1).replace("&amp;", "&"))).json()
        assert whole["total"] == sum(expected.values())
    assert "/operator/history/" not in section(html, "reconciliation_runs").split("<table")[1]


@pytest.mark.asyncio
async def test_series_are_drawn_in_neutral_ink_never_a_health_colour(tmp_path):
    application = history_app(tmp_path)
    seeded(tmp_path, trends_query("24h", NOW))
    html = (await get(application, headers=BROWSER)).text
    for key in DATA:
        drawn = section(html, key)
        assert "<svg" in drawn
        assert not re.search(r"tone-(ok|warn|crit)", drawn), key
        assert not re.search(r"\b(fill|stroke|style)=\"", drawn.replace('class="', "")), key
    css = (Path(trends_module.__file__).parent / "templates" / "operator.css").read_text()
    for rule in (".bar {", ".bar-open {", ".slot-open {", ".grid {", ".baseline {"):
        body = css[css.index(rule) : css.index("}", css.index(rule))]
        assert not re.search(r"--(ok|warn|crit|stop)", body), rule
    assert "fill: var(--chart-ink);" in css[css.index(".bar {") :]


@pytest.mark.asyncio
async def test_the_trends_page_is_structured_script_free_and_linked(tmp_path):
    application = history_app(tmp_path)
    seeded(tmp_path, trends_query("24h", NOW))
    for window in WINDOWS:
        html = (await get(application, headers=BROWSER, params={"window": window})).text
        parsed = _Tables()
        parsed.feed(html)
        assert parsed.headings[0] == 1 and parsed.headings.count(1) == 1
        assert all(b - a <= 1 for a, b in zip(parsed.headings, parsed.headings[1:], strict=False))
        assert parsed.visible_marks == 0, window
        assert "<script" not in html and "http" not in html.split("<main")[1]
        assert html.count("<link") == 1 and ICON_LINK in html, window
        assert f'<option value="{window}" selected>' in html
        nav = html[html.index('<nav class="history-nav"') : html.index("</nav>")]
        assert f'<a href="{PATH}" aria-current="page">Trends</a>' in nav
        assert "token" not in html.split("<main")[1].lower()
    one = (await get(application, headers=BROWSER)).text
    assert "1 completed run in this window" not in one  # seven, so plural
    for kind in ("orders", "risk"):
        page = (await get(application, PATHS[kind], headers=BROWSER)).text
        assert f'<a href="{PATH}">Trends</a>' in page
    dashboard = (await get(application, "/operator", headers=BROWSER)).text
    assert f'<a href="{PATH}">Trends</a>' in dashboard


@pytest.mark.asyncio
async def test_one_count_reads_in_the_singular(tmp_path):
    application = history_app(tmp_path)
    add(tmp_path, run(NOW))
    html = (await get(application, headers=BROWSER)).text
    assert '<span class="count">1</span> completed run in this window' in section(
        html, "reconciliation_runs"
    )


def test_the_scale_is_a_round_number_that_holds_the_peak():
    cases = {1: 1, 2: 2, 3: 4, 5: 6, 8: 8, 9: 10, 11: 20, 45: 60, 241: 400, 1000: 1000, 1001: 2000}
    assert {peak: _scale(peak) for peak in cases} == cases


@pytest.mark.asyncio
async def test_an_unrecognised_gate_is_drawn_under_the_key_it_was_recorded_with(tmp_path):
    application = history_app(tmp_path)
    add(tmp_path, decision(NOW, "retired_gate"), decision(NOW, None))
    refusals = section((await get(application, headers=BROWSER)).text, "refusals")
    # The row, its chart summary, and its table column.
    assert refusals.count("Unrecognised gate (retired_gate)") == 3
    assert '<span class="mono age">retired_gate</span>' not in refusals
    assert "No gate recorded" in refusals


# Read-only, authenticated, and indexed


@pytest.mark.asyncio
async def test_trends_only_read_and_need_an_operator(tmp_path):
    application = history_app(tmp_path)
    route = next(item for item in history_router.routes if item.path == PATH)
    assert route.methods <= {"GET", "HEAD"}
    async with client_for(application) as client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert (await client.request(method, PATH, headers=ADMIN)).status_code == 405
        assert (await client.get(PATH)).status_code == 401
        assert (await client.get(PATH, params={"token": OPERATOR_TOKEN})).status_code == 400
        await client.post(
            "/operator/login",
            data={"token": OPERATOR_TOKEN},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        page = await client.get(PATH, headers={"accept": "text/html"})
        assert page.status_code == 200 and "<dd>Operator</dd>" in page.text
    # The read model holds a session factory and nothing that can reach a broker.
    assert set(vars(application.state.trends)) == {"session_factory"}


def test_every_trends_read_searches_an_index_inside_the_window(tmp_path):
    trends = reader(tmp_path)
    seeded(tmp_path, trends_query("24h", NOW))
    engine = trends.session_factory.kw["bind"]
    statements: list[tuple[str, tuple]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement, parameters))

    for window in WINDOWS:
        statements.clear()
        event.listen(engine, "before_cursor_execute", capture)
        query = trends_query(window, NOW)
        trends.read(query)
        event.remove(engine, "before_cursor_execute", capture)
        assert len(statements) == 3
        tables = "portfolio_snapshots|discrepancies|risk_decisions"
        since = query.since.strftime("%Y-%m-%d %H:%M:%S")
        until = query.until.strftime("%Y-%m-%d %H:%M:%S")
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
                # Every time the database is given lies inside the capped window.
                times = [value for value in parameters if re.match(r"\d{4}-\d\d-\d\d ", str(value))]
                assert times and all(since <= value[:19] <= until for value in times), times
