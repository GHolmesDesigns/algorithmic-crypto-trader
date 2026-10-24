"""The dashboard's UTC clock (#163): server-anchored, honest without JavaScript, never in the way.

The kill-switch bar shows the server's UTC time so every age on the page can be read against it.
The markup carries the server's time and labels it "at page load"; a small same-origin script
then ticks it from that anchor, so a wrong device clock or time zone cannot change what is shown.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from api.dashboard import build_dashboard
from api.routes import UTC_CLOCK_FILE, UTC_CLOCK_PATH
from core.models import KillSwitchState

from tests.test_operator_dashboard import (
    ADMIN,
    OPERATOR,
    HealthyBroker,
    clock_script_url,
    dashboard_app,
    page,
    safety_bar,
)

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$")
SCRIPT = Path(UTC_CLOCK_FILE)
WORKSPACE_SCRIPT = (
    Path(__file__).resolve().parents[1] / "api" / "static" / "dashboard" / "operator-workspace.js"
)
NODE = shutil.which("node")


def clock_block(html: str) -> str:
    start = html.index('<div class="clock"')
    return html[start : html.index("</div>", start) + len("</div>")]


def controls_block(html: str) -> str:
    """The controls group, which is the last thing in the bar."""

    return html[html.index('<div class="controls"') :]


# The server-rendered clock


def test_the_view_carries_the_server_time_in_the_pages_stamp_format():
    now = datetime(2026, 10, 13, 17, 15, 30, 250_000, tzinfo=UTC)
    clock = build_dashboard({}, role="operator", now=now)["clock"]
    assert clock == {
        "iso": "2026-10-13T17:15:30.250000+00:00",
        "text": "2026-10-13 17:15:30 UTC",
        "epoch_ms": 1_791_911_730_250,
    }


def test_a_non_utc_or_naive_time_is_read_as_the_same_utc_instant():
    plus_two = timezone(timedelta(hours=2))
    aware = build_dashboard({}, role="operator", now=datetime(2026, 10, 13, 19, 0, tzinfo=plus_two))
    naive = build_dashboard({}, role="operator", now=datetime(2026, 10, 13, 17, 0))
    assert aware["clock"]["text"] == "2026-10-13 17:00:00 UTC"
    assert naive["clock"] == aware["clock"]


@pytest.mark.asyncio
async def test_the_bar_holds_the_server_time_left_of_the_controls(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    before = datetime.now(UTC)
    bar = safety_bar(await page(application))
    after = datetime.now(UTC)
    block = clock_block(bar)
    assert bar.index('class="safety-state"') < bar.index('class="clock"')
    assert bar.index('class="clock"') < bar.index('class="controls"')
    anchor = int(re.search(r'data-server-epoch-ms="(\d+)"', block)[1])
    assert int(before.timestamp() * 1000) - 1 <= anchor <= int(after.timestamp() * 1000) + 1
    text = re.search(r"<time [^>]*>([^<]+)</time>", block)[1]
    assert STAMP.match(text)
    served = datetime.fromtimestamp(anchor / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    assert text == served
    machine = datetime.fromisoformat(re.search(r'datetime="([^"]+)"', block)[1])
    assert machine.utcoffset() == timedelta(0)
    assert int(machine.timestamp() * 1000) == anchor


@pytest.mark.asyncio
async def test_without_javascript_the_stamp_says_it_is_from_page_load(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    block = clock_block(safety_bar(await page(application)))
    assert ">At page load</span>" in block
    assert 'aria-label="UTC time at page load"' in block
    assert "Now" not in block
    assert "live" not in block.lower()


@pytest.mark.asyncio
async def test_the_clock_has_a_time_element_and_is_not_a_live_region(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    block = clock_block(safety_bar(await page(application)))
    assert re.search(r'<time class="clock-time" datetime="[^"]+" aria-label="[^"]+"', block)
    for forbidden in ("aria-live", "aria-atomic", "role="):
        assert forbidden not in block
    # The decorative label is not read twice beside the time's own name.
    assert '<span class="eyebrow clock-label" aria-hidden="true"' in block


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, KillSwitchState.PAUSED, KillSwitchState.HALTED])
async def test_both_controls_keep_their_markup_in_every_state(monkeypatch, state):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    if state is not None:
        application.state.kill_switch.set_state(state, reason="operator action")
    for headers in (OPERATOR, ADMIN):
        html = await page(application, headers)
        bar = safety_bar(html)
        controls = controls_block(bar)
        assert "clock" not in controls
        assert '<form method="post" action="/operator/emergency-stop">' in controls
        assert "EMERGENCY STOP</button>" in controls
        if state is KillSwitchState.HALTED:
            assert 'action="/operator/pause"' not in html
            assert "PAUSE hidden: administrator re-arm required." in controls
        else:
            assert '<form method="post" action="/operator/pause">' in controls
        assert "data-utc-clock" in bar


@pytest.mark.asyncio
async def test_a_long_safety_text_leaves_the_clock_and_controls_in_the_bar(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    application.state.kill_switch.set_state(KillSwitchState.HALTED, reason="x " * 400)
    bar = safety_bar(await page(application))
    assert bar.index('class="safety-state"') < bar.index('class="clock"')
    assert bar.index('class="clock"') < bar.index('class="controls"')


@pytest.mark.asyncio
async def test_the_fragment_has_the_stamp_but_no_script(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    fragment = await page(application, path="/operator/fragment")
    assert ">At page load</span>" in fragment
    assert "<script" not in fragment.lower()


# The script and its route


@pytest.mark.asyncio
async def test_the_page_loads_one_hashed_same_origin_script_and_no_inline_code(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    html = await page(application)
    url = clock_script_url(html)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        served = await client.get(url)
    assert url == f"{UTC_CLOCK_PATH}?v={hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:12]}"
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("application/javascript")
    assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.content == SCRIPT.read_bytes()
    workspace_url = re.search(
        r'<script src="(/operator/static/operator-workspace\.js\?v=[0-9a-f]{12})" defer>',
        html,
    )[1]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        workspace_served = await client.get(workspace_url)
    assert workspace_url == (
        "/operator/static/operator-workspace.js?"
        f"v={hashlib.sha256(WORKSPACE_SCRIPT.read_bytes()).hexdigest()[:12]}"
    )
    assert workspace_served.status_code == 200
    assert workspace_served.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert workspace_served.content == WORKSPACE_SCRIPT.read_bytes()
    # No inline script or handler, and no policy was added or loosened by this card.
    assert re.findall(r"<script[^>]*>", html) == [
        f'<script src="{url}" defer>',
        re.search(r'<script src="/operator/static/operator-appearance\.js\?v=[0-9a-f]{12}">', html)[
            0
        ],
        f'<script src="{workspace_url}" defer>',
    ]
    assert re.search(r"\son[a-z]+=", html) is None
    assert "content-security-policy" not in {name.lower() for name in served.headers}


def test_workspace_script_only_controls_the_sidebar_preference():
    source = WORKSPACE_SCRIPT.read_text(encoding="utf-8")
    for name in (
        "fetch",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "innerHTML",
        "outerHTML",
        "document.write",
        "eval(",
        "Function(",
        "cookie",
    ):
        assert name not in source, name
    assert "localStorage" in source
    assert "act.operator.sidebar-collapsed.v1" in source


@pytest.mark.skipif(
    NODE is None, reason="Node is not installed; check sidebar behavior in a browser"
)
def test_sidebar_expands_by_default_persists_and_fails_open_when_storage_is_denied():
    harness = Path(__file__).parent / "fixtures" / "operator_workspace_harness.js"
    done = subprocess.run(
        [NODE, str(harness), str(WORKSPACE_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["initial"] == {
        "expanded": "true",
        "hidden": False,
        "label": "Collapse navigation",
        "collapsedClass": False,
    }
    assert result["collapsed"]["expanded"] == "false"
    assert result["collapsed"]["collapsedClass"] is True
    assert result["storedValue"] == "collapsed"
    assert result["reloaded"]["expanded"] == "false"
    assert result["denied"]["expanded"] == "true"
    assert result["denied"]["collapsedClass"] is False
    assert result["deniedWrite"]["expanded"] == "true"
    # The script marks the same module the server rendered, so System is not cleared on load.
    assert result["current"] == {
        "/operator": ["overview"],
        "/operator/system": ["system"],
        "/operator/rearm": ["system"],
        "/operator/diagnostics": ["system"],
        "/operator/watchlist": ["watch-coins"],
        "/operator/history/orders": ["activity"],
        "/operator/research": ["research"],
    }


def test_the_script_makes_no_request_and_writes_no_markup():
    source = SCRIPT.read_text(encoding="utf-8")
    for name in (
        "fetch",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "importScripts",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
        "localStorage",
        "sessionStorage",
        "cookie",
    ):
        assert name not in source, name
    assert "http://" not in source and "https://" not in source


# The tick and offset logic, run in Node when it is installed

HARNESS = Path(__file__).parent / "fixtures" / "clock_harness.js"


def run_harness(*, server_ms: int, device_ms: int, server_text: str) -> dict:
    payload = {"serverMs": server_ms, "deviceMs": device_ms, "serverText": server_text}
    done = subprocess.run(
        [NODE, str(HARNESS), str(SCRIPT), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="Node is not installed; check the clock in a browser")
def test_a_wrong_device_clock_does_not_change_the_time_shown():
    server_ms = int(datetime(2026, 10, 13, 17, 15, 30, 250_000, tzinfo=UTC).timestamp() * 1000)
    # The device is years and several hours off, as if its time zone were set as UTC.
    device_ms = int(datetime(2031, 3, 9, 2, 59, 59, 900_000, tzinfo=UTC).timestamp() * 1000)
    result = run_harness(
        server_ms=server_ms,
        device_ms=device_ms,
        server_text="2026-10-13 17:15:30 UTC",
    )
    pure, good = result["pure"], result["good"]
    assert pure["format"] == "2026-10-13 17:15:30 UTC"
    assert pure["formatBad"] == [None, None, None, None]
    assert pure["offset"] == server_ms - device_ms
    assert pure["serverNow"] == server_ms + 1234
    assert pure["waits"] == [1000, 999, 750, 1, 1000, 250, 1, 250]
    # On load it shows server time, labelled as live, and names itself.
    assert good["atLoad"] == "2026-10-13 17:15:30 UTC"
    assert good["label"] == "Now"
    assert good["name"] == "Current UTC time"
    # It advances on each whole second of server time, never skipping or repeating one.
    assert good["delays"] == [750, 1000, 1000]
    assert good["after1"] == "2026-10-13 17:15:31 UTC"
    assert good["after2"] == "2026-10-13 17:15:32 UTC"
    assert good["after3"] == "2026-10-13 17:15:33 UTC"
    # A page that slept for an hour catches up at once when it is shown again.
    assert good["afterSleep"] == "2026-10-13 18:15:33 UTC"
    assert good["datetime"] == "2026-10-13T18:15:33.000Z"


@pytest.mark.skipif(NODE is None, reason="Node is not installed; check the clock in a browser")
def test_an_unreadable_anchor_leaves_the_page_load_stamp_untouched():
    result = run_harness(
        server_ms=1_791_911_730_250, device_ms=0, server_text="2026-10-13 17:15:30 UTC"
    )
    bad = result["bad"]
    assert bad["atLoad"] == "2026-10-13 17:15:30 UTC"
    assert bad["label"] == "At page load"
    assert bad.get("name") is None
    assert bad["delays"] == []
