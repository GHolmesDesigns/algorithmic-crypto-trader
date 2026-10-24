"""Shared navigation and the sidebar preference remain display-only."""

import re

import httpx
import pytest

from tests.test_operator_dashboard import ADMIN, OPERATOR, HealthyBroker, dashboard_app


@pytest.mark.asyncio
async def test_existing_operator_pages_use_the_same_authenticated_workspace(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    destinations = (
        ("/operator", OPERATOR, "overview"),
        ("/operator/watchlist", OPERATOR, "watch-coins"),
        ("/operator/markets", OPERATOR, "watch-coins"),
        ("/operator/history/orders", OPERATOR, "activity"),
        ("/operator/history/trends", OPERATOR, "activity"),
        ("/operator/history/soak", ADMIN, "activity"),
        ("/operator/research", OPERATOR, "research"),
        ("/operator/rearm", ADMIN, "system"),
        ("/operator/system", OPERATOR, "system"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        for path, headers, active in destinations:
            response = await client.get(
                path, headers={**headers, "accept": "text/html,application/xhtml+xml"}
            )
            assert response.status_code in {200, 503}, (path, response.status_code, response.text)
            html = response.text
            assert 'id="workspace-nav" aria-label="Primary navigation"' in html
            assert "Mode" in html and "Credential scope" in html and "Signed in as" in html
            assert 'data-utc-clock data-server-epoch-ms="' in html
            assert '<section class="safety ' in html
            assert 'action="/operator/emergency-stop"' in html
            assert 'action="/operator/pause"' in html or "PAUSE hidden:" in html
            if active:
                assert re.search(
                    rf'<a href="[^"]+" data-module="{re.escape(active)}" '
                    r'aria-current="page">[^<]+</a>',
                    html,
                )
            if path in {"/operator/watchlist", "/operator/markets"}:
                assert '<nav class="history-nav" aria-label="Watch Coins views">' in html
                assert 'href="/operator/markets"' in html and 'href="/operator/watchlist"' in html
                current_path = path
                assert re.search(rf'<a href="{re.escape(current_path)}" aria-current="page">', html)


@pytest.mark.asyncio
async def test_navigation_and_controls_remain_server_rendered_without_javascript(monkeypatch):
    application = dashboard_app(monkeypatch, broker=HealthyBroker())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            "/operator/research", headers={**OPERATOR, "accept": "text/html"}
        )
    assert response.status_code == 200
    html = response.text
    for destination in (
        "/operator",
        "/operator/watchlist",
        "/operator/history/orders",
        "/operator/research",
        "/operator/system",
    ):
        assert f'href="{destination}"' in html
    assert 'aria-expanded="true" aria-controls="workspace-nav" hidden' in html
    assert '<form method="post" action="/operator/pause">' in html
    assert '<form method="post" action="/operator/emergency-stop">' in html
