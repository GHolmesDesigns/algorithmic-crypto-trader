"""Safety and browser-contract tests for the bounded research workspace."""

from __future__ import annotations

import re

import httpx
import pytest
from api.research import ResearchJob, ResearchJobStatus, ResearchRunRequest
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


@pytest.mark.asyncio
async def test_html_review_confirms_every_assumption_once_without_javascript(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    browser = {**OPERATOR, "accept": "text/html"}
    values = {
        "dataset_id": "btc-usd-high-volatility-v1",
        "strategy_version": "ma-cross-3-8-v1",
        "run_type": "backtest",
        "initial_cash": "123456.75",
        "quantity": "0.25",
        "maker_fee_rate": "0.001",
        "taker_fee_rate": "0.002",
        "spread_bps": "12.5",
        "slippage_bps": "7",
        "fee_asset": "BTC",
        "partial_fill_ratio": "0.75",
        "train_bars": "16",
        "test_bars": "16",
        "step_bars": "24",
        "holdout_bars": "8",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        setup = await client.get("/operator/research", headers=browser)
        assert setup.status_code == 200
        assert 'action="/operator/research/review"' in setup.text
        assert 'name="dataset_id"' in setup.text
        assert 'name="strategy_version"' in setup.text
        assert 'name="run_type"' in setup.text
        assert 'name="initial_cash"' not in setup.text.split("<details", 1)[0]
        assert "Fee rates default to zero" in setup.text
        assert "zero and do not represent realistic trading costs" in setup.text
        assert app.state.research.jobs == {}

        review = await client.post("/operator/research/review", data=values, headers=browser)
        assert review.status_code == 200
        assert "Review this simulation" in review.text
        assert "BTC-USD high volatility" in review.text
        assert "ma-cross-3-8-v1" in review.text
        assert "Replay through the simulator" not in review.text
        assert "123456.75 USD" in review.text
        assert "0.25" in review.text
        assert "0.001" in review.text and "0.002" in review.text
        assert "12.5 basis points" in review.text and "7 basis points" in review.text
        assert "BTC" in review.text and "0.75" in review.text
        assert "16 training · 16 test · 24 step · 8 sealed holdout bars" in review.text
        assert "of 5000 candles" in review.text
        assert "of 24 maximum" in review.text
        assert "Queue this simulation" in review.text
        assert app.state.research.jobs == {}

        review_id = re.search(r'name="review_id" value="([^"]+)"', review.text)
        assert review_id is not None
        confirmation = {
            **values,
            "confirmation": "queue",
            "review_id": review_id.group(1),
        }
        queued = await client.post("/operator/research/runs", data=confirmation, headers=browser)
        assert queued.status_code == 303
        run_id = queued.headers["location"].rsplit("/", 1)[-1]
        job = await app.state.research.wait(run_id)
        assert job.request.to_dict() == {
            **values,
            "run_type": "backtest",
            "train_bars": 16,
            "test_bars": 16,
            "step_bars": 24,
            "holdout_bars": 8,
        }
        assert job.status is ResearchJobStatus.COMPLETE

        duplicate = await client.post("/operator/research/runs", data=confirmation, headers=browser)
        assert duplicate.status_code == 422
        assert "already used" in duplicate.text
        assert 'value="123456.75"' in duplicate.text
        assert 'value="0.75"' in duplicate.text
        assert list(app.state.research.jobs) == [run_id]


@pytest.mark.asyncio
async def test_invalid_review_keeps_values_and_does_not_start_a_job(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    browser = {**OPERATOR, "accept": "text/html"}
    values = {
        "dataset_id": "unapproved-dataset",
        "strategy_version": "unapproved-strategy",
        "run_type": "unsupported-run-type",
        "initial_cash": "not-a-number",
        "quantity": "0.5",
        "maker_fee_rate": "0.001",
        "taker_fee_rate": "0",
        "spread_bps": "4",
        "slippage_bps": "5",
        "fee_asset": "USD",
        "partial_fill_ratio": "0.5",
        "train_bars": "20",
        "test_bars": "16",
        "step_bars": "",
        "holdout_bars": "4",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/operator/research/review", data=values, headers=browser)
    assert response.status_code == 422
    assert "The run was not started" in response.text
    assert 'value="not-a-number"' in response.text
    assert 'value="0.5"' in response.text
    assert 'value="20"' in response.text
    assert 'value="4"' in response.text
    assert 'value="unapproved-dataset" selected' in response.text
    assert 'value="unapproved-strategy" selected' in response.text
    assert 'value="unsupported-run-type" selected' in response.text
    assert app.state.research.jobs == {}


@pytest.mark.asyncio
async def test_confirmation_rechecks_current_queue_limit_and_preserves_values(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    browser = {**OPERATOR, "accept": "text/html"}
    values = {
        "dataset_id": "btc-usd-calm-trend-v1",
        "strategy_version": "always-buy-v1",
        "run_type": "replay",
        "initial_cash": "222000",
        "quantity": "0.75",
        "maker_fee_rate": "0.001",
        "taker_fee_rate": "0.002",
        "spread_bps": "10",
        "slippage_bps": "6",
        "fee_asset": "USD",
        "partial_fill_ratio": "1",
        "train_bars": "16",
        "test_bars": "16",
        "step_bars": "",
        "holdout_bars": "8",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        review = await client.post("/operator/research/review", data=values, headers=browser)
        review_id = re.search(r'name="review_id" value="([^"]+)"', review.text)
        assert review_id is not None
        for index in range(2):
            existing_request = ResearchRunRequest(
                dataset_id="btc-usd-calm-trend-v1", strategy_version="always-buy-v1"
            )
            app.state.research.jobs[f"existing-{index}"] = ResearchJob(
                run_id=f"existing-{index}",
                request=existing_request,
                status=ResearchJobStatus.RUNNING,
            )
        response = await client.post(
            "/operator/research/runs",
            data={**values, "confirmation": "queue", "review_id": review_id.group(1)},
            headers=browser,
        )
    assert response.status_code == 422
    assert "research queue is full" in response.text
    assert 'value="222000"' in response.text
    assert 'value="0.75"' in response.text
    assert set(app.state.research.jobs) == {"existing-0", "existing-1"}


@pytest.mark.asyncio
async def test_confirmation_rejects_changed_assumptions_and_keeps_them_editable(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    transport = httpx.ASGITransport(app=app)
    browser = {**OPERATOR, "accept": "text/html"}
    values = {
        "dataset_id": "btc-usd-calm-trend-v1",
        "strategy_version": "always-buy-v1",
        "run_type": "backtest",
        "initial_cash": "100000",
        "quantity": "1",
        "maker_fee_rate": "0.001",
        "taker_fee_rate": "0.002",
        "spread_bps": "10",
        "slippage_bps": "5",
        "fee_asset": "USD",
        "partial_fill_ratio": "1",
        "train_bars": "16",
        "test_bars": "16",
        "step_bars": "",
        "holdout_bars": "8",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        review = await client.post("/operator/research/review", data=values, headers=browser)
        review_id = re.search(r'name="review_id" value="([^"]+)"', review.text)
        assert review_id is not None
        changed = await client.post(
            "/operator/research/runs",
            data={
                **values,
                "quantity": "2",
                "confirmation": "queue",
                "review_id": review_id.group(1),
            },
            headers=browser,
        )
    assert changed.status_code == 422
    assert "submitted assumptions changed" in changed.text
    assert 'value="2"' in changed.text
    assert app.state.research.jobs == {}


@pytest.mark.asyncio
async def test_recent_run_statuses_and_direct_links_remain_visible(monkeypatch):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    app = application()
    request = ResearchRunRequest(
        dataset_id="btc-usd-calm-trend-v1", strategy_version="always-buy-v1"
    )
    statuses = (
        ResearchJobStatus.QUEUED,
        ResearchJobStatus.RUNNING,
        ResearchJobStatus.COMPLETE,
        ResearchJobStatus.FAILED,
    )
    for status in statuses:
        run_id = f"run-{status.value}"
        app.state.research.jobs[run_id] = ResearchJob(
            run_id=run_id,
            request=request,
            status=status,
            error="fixture failure" if status is ResearchJobStatus.FAILED else None,
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/operator/research", headers={**OPERATOR, "accept": "text/html"}
        )
        failed_detail = await client.get(
            "/operator/research/runs/run-failed",
            headers={**OPERATOR, "accept": "text/html"},
        )
    assert response.status_code == 200
    for status in statuses:
        assert f">{status.value}</td>" in response.text
        assert f'href="/operator/research/runs/run-{status.value}"' in response.text
    assert failed_detail.status_code == 200
    assert "fixture failure" in failed_detail.text
    assert 'value="run-complete"' in response.text
    assert 'value="run-queued" disabled' in response.text
    assert 'value="run-running" disabled' in response.text
    assert 'value="run-failed" disabled' in response.text
