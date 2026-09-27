"""Safety and browser-contract tests for the bounded research workspace."""

from __future__ import annotations

import httpx
import pytest
from app.main import create_app
from core.guards import CredentialScope, StartupSettings
from core.models import TradingMode

OPERATOR = {"x-operator-token": "operator-secret"}


def application():
    return create_app(
        StartupSettings(
            TradingMode.BACKTEST, CredentialScope.NONE, "", "sqlite+pysqlite://", "INFO"
        )
    )


async def submit(
    client: httpx.AsyncClient, strategy: str = "always-buy-v1", run_type: str = "backtest"
):
    response = await client.post(
        "/operator/research/runs",
        headers=OPERATOR,
        json={
            "dataset_id": "btc-usd-calm-trend-v1",
            "strategy_version": strategy,
            "run_type": run_type,
            "train_bars": 16,
            "test_bars": 16,
            "holdout_bars": 8,
        },
    )
    assert response.status_code == 202
    return response.json()["run_id"]


@pytest.mark.asyncio
async def test_research_catalog_requires_authentication_and_contains_approved_datasets(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/operator/research")).status_code == 401
        response = await client.get("/operator/research", headers=OPERATOR)
    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "Research and replay"
    assert {item["id"] for item in payload["catalog"]} == {
        "btc-usd-calm-trend-v1",
        "btc-usd-high-volatility-v1",
    }
    assert {item["version"] for item in payload["strategies"]} == {
        "always-buy-v1",
        "ma-cross-3-8-v1",
    }


@pytest.mark.asyncio
async def test_backtest_job_uses_report_fields_and_never_needs_a_broker(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await submit(client)
        job = await app.state.research.wait(run_id)
        assert job.status.value == "complete"
        assert job.result is not None
        report = job.result["report"]
        assert report["strategy_version"] == "always-buy-v1"
        assert report["trade_count"] >= 1
        assert report["per_year"]
        assert report["per_regime"]
        assert job.result["disclaimer"] == "Past results are not a promise of profit."
        detail = await client.get(f"/operator/research/runs/{run_id}", headers=OPERATOR)
        assert detail.status_code == 200


@pytest.mark.asyncio
async def test_research_job_does_not_call_configured_broker(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")

    class FailingBroker:
        calls = 0

        async def get_balances(self):
            self.calls += 1
            raise AssertionError("research must not read a broker")

        async def get_positions(self):
            self.calls += 1
            raise AssertionError("research must not read a broker")

    broker = FailingBroker()
    app = create_app(
        StartupSettings(TradingMode.PAPER, CredentialScope.NONE, "", "sqlite+pysqlite://", "INFO"),
        broker=broker,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await submit(client, strategy="ma-cross-3-8-v1")
        job = await app.state.research.wait(run_id)
    assert job.status.value == "complete"
    assert broker.calls == 0


@pytest.mark.asyncio
async def test_replay_export_is_redacted_and_comparison_is_versioned(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await submit(client, strategy="always-buy-v1", run_type="replay")
        second = await submit(client, strategy="ma-cross-3-8-v1")
        await app.state.research.wait(first)
        await app.state.research.wait(second)
        compared = await client.get(
            "/operator/research/compare",
            params=[("run", first), ("run", second)],
            headers=OPERATOR,
        )
        exported = await client.get(f"/operator/research/runs/{first}/export", headers=OPERATOR)
    assert compared.status_code == 200
    assert len(compared.json()["compare"]) == 2
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/json")
    body = exported.text
    assert "Past results are not a promise of profit." in body
    assert "operator-secret" not in body
    assert "provider payload" not in body.lower()
    assert "credential" not in body.lower()
    assert "http://" not in body and "https://" not in body


@pytest.mark.asyncio
async def test_invalid_dataset_and_unbounded_walk_forward_are_refused(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post(
            "/operator/research/runs",
            headers=OPERATOR,
            json={"dataset_id": "not-approved", "strategy_version": "always-buy-v1"},
        )
        too_many = await client.post(
            "/operator/research/runs",
            headers=OPERATOR,
            json={
                "dataset_id": "btc-usd-calm-trend-v1",
                "strategy_version": "always-buy-v1",
                "train_bars": 1,
                "test_bars": 1,
                "step_bars": 1,
            },
        )
    assert missing.status_code == 422
    assert too_many.status_code == 422
    assert "windows" in too_many.json()["detail"]["errors"][0]


@pytest.mark.asyncio
async def test_html_workspace_carries_disclaimer_and_navigation(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/operator/research",
            headers={**OPERATOR, "accept": "text/html"},
        )
    assert response.status_code == 200
    assert "Research and replay" in response.text
    assert "Past results are not a promise of profit" in response.text
    assert "never contact a broker" in response.text
    assert "/operator/history/orders" in response.text
