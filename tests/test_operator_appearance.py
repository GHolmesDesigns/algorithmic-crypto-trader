"""Operator appearance (#177): Light, Dark or Follow device, kept in the browser and nowhere else.

The behaviour protected here: the System page offers the three choices, a choice applies to every
operator page and survives navigation and reload, Follow device tracks the device and an explicit
choice overrides it, and a missing, corrupt or blocked store - or no JavaScript - still leaves a
readable supported theme. Choosing an appearance never reaches the server, a provider or any
trading or safety state.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from api.routes import APPEARANCE_SCRIPT_FILE, APPEARANCE_SCRIPT_PATH

from tests.test_operator_dashboard import (
    ADMIN,
    CSS,
    OPERATOR,
    HealthyBroker,
    dashboard_app,
    section,
)
from tests.test_operator_system import BROWSER, SYSTEM, ReadOnlyBroker, fetch

SCRIPT = Path(APPEARANCE_SCRIPT_FILE)
MARKETS_SCRIPT = Path(__file__).resolve().parents[1] / "api" / "static" / "markets" / "markets.js"
HARNESS = Path(__file__).parent / "fixtures" / "operator_appearance_harness.js"
NODE = shutil.which("node")
KEY = "act.operator.appearance.v1"
SCRIPT_TAG = re.compile(
    r'<script src="(/operator/static/operator-appearance\.js\?v=[0-9a-f]{12})">'
)


@pytest.fixture(autouse=True)
def tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill-switch.json"))


# The System page section


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [OPERATOR, ADMIN])
async def test_system_offers_light_dark_and_follow_device_with_follow_device_selected(
    monkeypatch, headers
):
    html = await fetch(dashboard_app(monkeypatch, broker=ReadOnlyBroker()), headers)
    appearance = section(html, "appearance")
    assert '<h2 id="appearance-heading">Appearance</h2>' in appearance
    choices = re.findall(
        r'<label><input type="radio" name="appearance" value="(\w+)"( checked)?>'
        r"<span>([^<]+)<span",
        appearance,
    )
    assert [(value, label) for value, _checked, label in choices] == [
        ("light", "Light mode"),
        ("dark", "Dark mode"),
        ("system", "Follow device"),
    ]
    # Follow device is the default, and is the only choice selected.
    assert [value for value, checked, _label in choices if checked] == ["system"]
    assert '<legend class="visually-hidden">Appearance</legend>' in appearance


@pytest.mark.asyncio
async def test_the_section_says_the_choice_is_local_and_changes_nothing_else(monkeypatch):
    html = await fetch(dashboard_app(monkeypatch, broker=ReadOnlyBroker()))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", section(html, "appearance")))
    assert "kept in this browser only" in text
    assert "not a trading setting" in text and "not stored on the server" in text
    assert "feed, a provider, the kill switch or any order" in text


@pytest.mark.asyncio
async def test_without_javascript_the_choices_are_hidden_and_the_page_says_it_follows_the_device(
    monkeypatch,
):
    html = await fetch(dashboard_app(monkeypatch, broker=ReadOnlyBroker()))
    appearance = section(html, "appearance")
    # The choices stay hidden until the script runs, so no inert control is offered.
    assert re.search(
        r'<fieldset class="appearance-choices" data-appearance-group hidden>', appearance
    )
    assert re.search(r"<p [^>]*data-appearance-fallback>Choices need JavaScript", appearance)
    assert "follows this device's light or dark setting" in appearance
    # The control is plain markup: no form, no request, nothing to submit.
    assert "<form" not in appearance and "action=" not in appearance
    assert "<button" not in appearance


@pytest.mark.asyncio
async def test_the_status_line_is_polite_and_empty_until_the_script_fills_it(monkeypatch):
    html = await fetch(dashboard_app(monkeypatch, broker=ReadOnlyBroker()))
    assert '<p class="note" data-appearance-status aria-live="polite"></p>' in section(
        html, "appearance"
    )


# The shared script on every operator page


@pytest.mark.asyncio
async def test_every_operator_page_loads_the_script_before_the_body_paints(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    paths = (
        ("/operator", OPERATOR),
        ("/operator/watchlist", OPERATOR),
        ("/operator/markets", OPERATOR),
        ("/operator/history/orders", OPERATOR),
        ("/operator/history/trends", OPERATOR),
        ("/operator/history/soak", ADMIN),
        ("/operator/research", OPERATOR),
        ("/operator/rearm", ADMIN),
        (SYSTEM, OPERATOR),
        ("/operator/login", {}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        for path, headers in paths:
            response = await client.get(path, headers={**headers, **BROWSER})
            assert response.status_code in {200, 503}, (path, response.status_code)
            html = response.text
            tags = SCRIPT_TAG.findall(html)
            assert len(tags) == 1, path
            # Blocking, in the head: the theme is set before the first paint, so no flash.
            assert html.index(tags[0]) < html.index("<body"), path
            assert "<style>" in html and html.index(tags[0]) < html.index("<style>"), path


@pytest.mark.asyncio
async def test_the_script_is_served_from_the_app_without_a_login_and_cached_by_content(
    monkeypatch,
):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        page = await client.get("/operator/login", headers=BROWSER)
        url = SCRIPT_TAG.search(page.text)[1]
        served = await client.get(url)
    digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:12]
    assert url == f"{APPEARANCE_SCRIPT_PATH}?v={digest}"
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("application/javascript")
    assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.content == SCRIPT.read_bytes()


@pytest.mark.asyncio
async def test_no_route_accepts_an_appearance_choice(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    writes = {
        route.path
        for route in application.routes
        if "appearance" in getattr(route, "path", "")
        and (getattr(route, "methods", None) or set()) - {"GET", "HEAD"}
    }
    assert writes == set()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        for path in ("/operator/appearance", "/operator/settings/appearance"):
            response = await client.post(path, headers=ADMIN, data={"appearance": "dark"})
            assert response.status_code in {404, 405}


def test_the_script_touches_only_the_browser_local_preference():
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
        "sessionStorage",
        "indexedDB",
        "cookie",
        "location",
        "submit",
    ):
        assert name not in source, name
    assert "http://" not in source and "https://" not in source
    assert source.count("localStorage") == 2  # one read, one write
    assert f'"{KEY}"' in source
    # It sets and clears exactly one attribute and has no other write to the page.
    assert source.count("setAttribute") == 1 and source.count("removeAttribute") == 1
    assert "data-theme" in source


# The palette


def tokens_in(css: str, selector: str) -> dict[str, str]:
    start = css.index(f"{selector} {{")
    body = css[start : css.index("}", start)]
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6});", body))


def test_the_stylesheet_has_one_dark_palette_for_the_device_query_and_the_explicit_choice():
    css = CSS.read_text(encoding="utf-8")
    light = tokens_in(css, ":root")
    from_device = tokens_in(css, ':root:not([data-theme="light"])')
    explicit = tokens_in(css, ':root[data-theme="dark"]')
    assert from_device and from_device == explicit
    assert set(explicit) <= set(light)
    # An explicit Light choice is excluded from the device query, so a dark device cannot win.
    assert re.search(
        r"@media \(prefers-color-scheme: dark\) \{\s+:root:not\(\[data-theme=\"light\"\]\) \{", css
    )
    # Native controls and scrollbars follow the chosen scheme, not only the device.
    assert re.search(r':root\[data-theme="light"\] \{\s+color-scheme: light;', css)
    assert re.search(r':root\[data-theme="dark"\] \{\s+color-scheme: dark;', css)
    assert '<meta name="color-scheme" content="light dark">' in (
        Path(__file__).resolve().parents[1] / "api" / "templates" / "_operator_page.html"
    ).read_text(encoding="utf-8")


def _luminance(colour: str) -> float:
    channels = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _pairs() -> list[tuple[str, str]]:
    pairs = [
        (text, ground)
        for text in ("text", "text-2")
        for ground in ("ground", "surface", "surface-2")
    ]
    pairs += [("accent-text", "accent-soft"), ("accent-text", "surface"), ("accent", "surface")]
    for state in ("ok", "warn", "crit", "unknown", "neutral"):
        pairs += [(state, f"{state}-soft"), (state, "surface"), (state, "ground")]
    pairs += [
        ("stop-text", "stop"),
        ("rail-text", "rail-bg"),
        ("rail-text", "rail-active"),
        ("rail-accent", "rail-bg"),
        ("focus", "ground"),
        ("focus", "surface"),
    ]
    return pairs


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_status_words_and_focus_keep_aa_contrast_in_both_themes(theme):
    css = CSS.read_text(encoding="utf-8")
    palette = tokens_in(css, ":root")
    if theme == "dark":
        palette = {**palette, **tokens_in(css, ':root[data-theme="dark"]')}
    failures = [
        (foreground, background, round(_contrast(palette[foreground], palette[background]), 2))
        for foreground, background in _pairs()
        if _contrast(palette[foreground], palette[background]) < 4.5
    ]
    assert failures == []


def test_status_never_relies_on_colour_alone_in_either_theme():
    css = CSS.read_text(encoding="utf-8")
    # The stylesheet states the rule, and no appearance rule introduces a colour-only state.
    assert "Colour never carries state alone" in css
    appearance_rules = css[css.index("Appearance. Follow device") : css.index("*,\n*::before")]
    assert "tone-" not in appearance_rules and "pill" not in appearance_rules


# The Markets chart


def test_the_markets_chart_draws_in_the_palette_the_page_is_showing():
    source = MARKETS_SCRIPT.read_text(encoding="utf-8")
    # It reads the page's own tokens instead of choosing a palette from the device alone.
    for name in ("--surface", "--text", "--line-soft", "--line", "--chart-ink"):
        assert f'token("{name}"' in source
    assert "getComputedStyle(document.documentElement)" in source
    assert 'attributeFilter: ["data-theme"]' in source
    assert 'darkMode.addEventListener("change", restyle)' in source
    assert "darkMode.matches\n      ?" not in source


# The script's behaviour, run in Node when it is installed


@pytest.mark.skipif(NODE is None, reason="Node is not installed; check appearance in a browser")
def test_choices_apply_persist_and_fail_open():
    done = subprocess.run(
        [NODE, str(HARNESS), str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    follow = "Following this device, which is currently {}."
    stored_note = "This browser blocks storage, so the choice is not saved"

    # Nothing stored: no override, Follow device selected, and the choices appear.
    assert result["fresh"] == {
        "theme": None,
        "checked": ["system"],
        "shown": {"group": True, "fallback": False},
        "status": follow.format("light"),
    }

    # Each of the three choices changes the appearance as labeled.
    assert result["dark"]["theme"] == "dark" and result["dark"]["checked"] == ["dark"]
    assert result["dark"]["stored"] == "dark"
    assert result["dark"]["status"] == "Dark mode is on for every operator page."
    assert result["light"]["theme"] == "light" and result["light"]["checked"] == ["light"]
    assert result["light"]["stored"] == "light"
    assert result["system"]["theme"] is None and result["system"]["checked"] == ["system"]
    assert result["system"]["stored"] == "system"

    # After navigating or reloading, the choice is applied on the page's first line of script,
    # before the document is parsed, and the control shows it once the page has loaded.
    assert result["reloadBeforeParsing"] == {
        "theme": "dark",
        "shown": {"group": False, "fallback": True},
    }
    assert result["reloadAfterParsing"]["theme"] == "dark"
    assert result["reloadAfterParsing"]["checked"] == ["dark"]
    assert result["afterSystem"]["theme"] is None
    assert result["ignoredOtherControl"] == "light"

    # Follow device responds to the device and sets no override; Light or Dark ignores it.
    assert result["followingDark"]["theme"] is None
    assert result["followingDark"]["status"] == follow.format("dark")
    assert result["followingLight"]["status"] == follow.format("light")
    assert result["followingLight"]["theme"] is None
    assert result["explicitLightOnDarkDevice"]["theme"] == "light"
    assert result["explicitLightOnDarkDevice"]["status"] == (
        "Light mode is on for every operator page."
    )

    # Corrupt or foreign values mean Follow device.
    assert result["corrupt"] == [None] * 7

    # Blocked storage keeps a readable theme, still lets the choice apply, and says it is unsaved.
    assert result["deniedRead"]["theme"] is None
    assert result["deniedRead"]["checked"] == ["system"]
    assert stored_note in result["deniedRead"]["status"]
    for case in ("deniedChoice", "missingStorage", "deniedWrite"):
        assert stored_note in result[case]["status"], case
    assert result["deniedChoice"]["theme"] == "dark"
    assert result["missingStorage"]["theme"] == "light"
    assert result["deniedWrite"]["theme"] == "dark"

    # Another tab's change is followed; an unrelated key is ignored.
    assert result["otherTabIgnored"]["theme"] is None
    assert result["otherTabFollowed"]["theme"] == "dark"
    assert result["otherTabFollowed"]["checked"] == ["dark"]

    # Pages without the section, and browsers without matchMedia, still apply the choice.
    assert result["noSection"] == "dark"
    assert result["noMatchMedia"]["theme"] == "light"
    assert result["noMatchMediaSystem"]["theme"] is None
    assert result["noMatchMediaSystem"]["status"] == follow.format("light")
