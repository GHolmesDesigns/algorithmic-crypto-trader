"""Owner-run exchange probes, driven against fakes: no test contacts an exchange."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from probes import coinbase_readonly_reconcile, coinbase_sandbox_capture, gemini_sandbox_lifecycle
from probes.common import (
    BoundedTransport,
    BudgetExhausted,
    ProbeRefused,
    ProbeReport,
    UnexpectedHost,
    bounded_client,
    prompt_secret,
    run_probe,
)

SANDBOX_FIXTURES = Path(__file__).parent / "fixtures" / "coinbase" / "sandbox"


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


# --- shared rules -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_transport_allows_only_named_hosts_and_a_fixed_budget() -> None:
    client, transport = bounded_client(
        hosts={"example.test"}, budget=3, reserve=1, inner=httpx.MockTransport(ok)
    )
    with pytest.raises(UnexpectedHost):
        await client.get("https://elsewhere.test/")
    await client.get("https://example.test/a")
    await client.get("https://example.test/b")
    with pytest.raises(BudgetExhausted):
        await client.get("https://example.test/c")
    transport.release_reserve()  # cleanup may spend the held-back request
    await client.get("https://example.test/cleanup")
    with pytest.raises(BudgetExhausted):
        await client.get("https://example.test/d")
    await client.aclose()
    assert transport.requests == [("GET", "/a"), ("GET", "/b"), ("GET", "/cleanup")]
    with pytest.raises(ValueError):
        BoundedTransport(httpx.MockTransport(ok), hosts=(), budget=2, reserve=2)


def test_a_result_that_would_contain_a_credential_is_never_written(tmp_path) -> None:
    # A PEM-style block: the dashed header lines are public; the body is the secret.
    block = "-----BEGIN TEST CREDENTIAL-----\nSecretBodyMaterial123\n-----END TEST CREDENTIAL-----"
    report = ProbeReport(probe="unit", target="https://example.test")
    report.keep_out("key-id-123456", block, "short")
    report.step("safe", "pass", count=3, header="-----BEGIN TEST CREDENTIAL-----")
    path = report.write(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["steps"][0]["count"] == 3
    report.step("leak", "fail", detail="SecretBodyMaterial123")
    with pytest.raises(ProbeRefused, match="credential"):
        report.write(tmp_path)


def test_secrets_come_only_from_a_hidden_prompt() -> None:
    assert prompt_secret("API key", ask=lambda _: "  typed-secret \n") == "typed-secret"
    with pytest.raises(ProbeRefused, match="no API key"):
        prompt_secret("API key", ask=lambda _: "   ")


def test_run_probe_saves_the_result_and_reports_the_outcome(tmp_path, capsys) -> None:
    async def passing() -> ProbeReport:
        report = ProbeReport(probe="unit", target="https://example.test", outcome="pass")
        report.step("one", "pass")
        report.notes.append("a note")
        return report

    async def failing() -> ProbeReport:
        return ProbeReport(probe="unit-fail", target="https://example.test", outcome="failed")

    async def refused() -> ProbeReport:
        raise ProbeRefused("not a View-only key")

    assert run_probe(passing, results=tmp_path) == 0
    assert run_probe(failing, results=tmp_path) == 1
    assert run_probe(refused, results=tmp_path) == 2
    printed = capsys.readouterr().out
    assert "unit: PASS" in printed and "note: a note" in printed
    assert "Stopped: not a View-only key" in printed
    assert len(list(tmp_path.glob("*.json"))) == 2


# --- check 3: Coinbase sandbox capture ------------------------------------------------


def sandbox_replay(broken: str | None = None):
    """Serve the committed sandbox captures back, as the sandbox itself would."""

    fixtures = {
        name: json.loads((SANDBOX_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        for name in json.loads((SANDBOX_FIXTURES / "manifest.json").read_text())["requests"]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers  # the sandbox needs no credentials
        path = request.url.path.removeprefix("/api/v3/brokerage")
        variant = request.headers.get("x-sandbox")
        for name, fixture in fixtures.items():
            asked = fixture["request"]
            if (asked["method"], asked["path"], asked.get("x_sandbox")) == (
                request.method,
                path,
                variant,
            ):
                if name == broken:
                    return httpx.Response(500, json={"error": "INTERNAL"})
                return httpx.Response(fixture["status"], json=fixture["response"])
        if path.startswith("/orders/historical/") and path != "/orders/historical/batch":
            return httpx.Response(400, json={"error": "INVALID_ARGUMENT"})
        raise AssertionError(f"unexpected {request.method} {path} {variant}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_the_sandbox_capture_writes_every_response_and_a_manifest(tmp_path) -> None:
    report = await coinbase_sandbox_capture.capture(tmp_path, inner=sandbox_replay())

    assert report.outcome == "pass"
    assert report.requests_made == 11 <= report.request_budget
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["authentication"].startswith("none")
    for name in manifest["requests"]:
        written = json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8"))
        assert written == json.loads((SANDBOX_FIXTURES / f"{name}.json").read_text()) | {
            "request": written["request"]
        }
    variants = {
        json.loads((tmp_path / f"{name}.json").read_text())["request"].get("x_sandbox")
        for name in manifest["requests"]
    }
    assert {"PostOrder_insufficient_fund", "CancelOrders_failure", "EditOrder_failure"} < variants


@pytest.mark.asyncio
async def test_a_failed_sandbox_request_marks_the_capture_incomplete(tmp_path) -> None:
    report = await coinbase_sandbox_capture.capture(tmp_path, inner=sandbox_replay("list_orders"))
    assert report.outcome == "incomplete"
    assert {"step": "list_orders", "result": "HTTP 500", "status": 500} in report.steps


# --- check 1: Gemini Sandbox lifecycle ----------------------------------------------


class FakeGeminiSandbox:
    """Balances, tickers, a one-level ask book, candles, and orders in Gemini's shapes."""

    def __init__(
        self,
        *,
        market_orders: bool = True,
        ask_depth: str = "0.002",
        fail_first_cancel: bool = False,
    ) -> None:
        self.market_orders = market_orders
        self.ask = Decimal("60010")
        self.ask_depth = Decimal(ask_depth)
        self.fail_first_cancel = fail_first_cancel
        self.orders: dict[int, dict[str, Any]] = {}
        self.by_client: dict[str, int] = {}
        self._ids = count(8_000_000_001)
        self._tids = count(1)
        self.seen_keys: set[str] = set()

    def live(self) -> list[dict[str, Any]]:
        return [order for order in self.orders.values() if order["is_live"]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.sandbox.gemini.com"
        path = request.url.path
        if request.method == "GET":
            return self.public(path)
        self.seen_keys.add(request.headers["x-gemini-apikey"])
        payload = json.loads(base64.b64decode(request.headers["x-gemini-payload"]))
        if path == "/v1/balances":
            return httpx.Response(
                200,
                json=[
                    {"currency": "BTC", "amount": "10", "available": "10"},
                    {"currency": "ETH", "amount": "20", "available": "20"},
                    {"currency": "USD", "amount": "100000", "available": "100000"},
                ],
            )
        if path == "/v1/order/new":
            return self.new_order(payload)
        if path == "/v1/order/status":
            order = self.find(payload)
            if order is None:
                return httpx.Response(404, json={"result": "error", "reason": "OrderNotFound"})
            return httpx.Response(200, json=self.body(order, trades=True))
        if path == "/v1/order/cancel":
            if self.fail_first_cancel:
                self.fail_first_cancel = False
                return httpx.Response(500, json={"result": "error", "reason": "System"})
            order = self.orders[int(payload["order_id"])]
            order.update(is_live=False, is_cancelled=True)
            return httpx.Response(200, json=self.body(order, trades=False))
        raise AssertionError(path)

    def public(self, path: str) -> httpx.Response:
        if path.startswith("/v2/ticker/"):
            symbol = path.rsplit("/", 1)[-1]
            price = {"btcusd": Decimal("60000"), "ethusd": Decimal("2500")}[symbol]
            ask = self.ask if symbol == "btcusd" else price + 1
            return httpx.Response(200, json={"bid": str(price), "ask": str(ask)})
        if path == "/v1/book/btcusd":
            level = {"price": str(self.ask), "amount": str(self.ask_depth), "timestamp": "1"}
            return httpx.Response(200, json={"bids": [], "asks": [level]})
        if path == "/v2/candles/btcusd/5m":
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            start = now - timedelta(minutes=now.minute % 5)
            rows = []
            for k in range(0, 12):  # the first row is the bar still in progress
                opened = start - timedelta(minutes=5 * k)
                rows.append([int(opened.timestamp() * 1000), 60000, 60020, 59990, 60005, 3.2])
            return httpx.Response(200, json=rows)
        raise AssertionError(path)

    def find(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        order_id = payload.get("order_id") or self.by_client.get(payload.get("client_order_id"))
        return self.orders.get(order_id) if order_id is not None else None

    def new_order(self, payload: dict[str, Any]) -> httpx.Response:
        amount = Decimal(payload["amount"])
        if payload["type"] == "exchange market" and not self.market_orders:
            return httpx.Response(
                400, json={"result": "error", "reason": "InvalidOrderType", "message": "no"}
            )
        if amount < Decimal("0.00001"):
            return httpx.Response(
                400, json={"result": "error", "reason": "InvalidQuantity", "message": "small"}
            )
        order_id = next(self._ids)
        order = {
            "id": order_id,
            "client_order_id": payload["client_order_id"],
            "side": payload["side"],
            "original": amount,
            "price": payload.get("price", "0"),
            "executed": Decimal("0"),
            "is_live": True,
            "is_cancelled": False,
            "trades": [],
        }
        self.orders[order_id] = order
        self.by_client[payload["client_order_id"]] = order_id
        crosses = payload["type"] == "exchange market" or Decimal(payload["price"]) >= self.ask
        if crosses:
            fill = amount if payload["type"] == "exchange market" else min(amount, self.ask_depth)
            order["trades"].append(
                {
                    "tid": next(self._tids),
                    "price": str(self.ask),
                    "amount": str(fill),
                    "fee_amount": "0.01",
                    "fee_currency": "USD",
                    "timestampms": 1727179200000,
                }
            )
            order["executed"] = fill
            order["is_live"] = fill < amount
        return httpx.Response(200, json=self.body(order, trades=False))

    @staticmethod
    def body(order: dict[str, Any], *, trades: bool) -> dict[str, Any]:
        result = {
            "order_id": str(order["id"]),
            "client_order_id": order["client_order_id"],
            "symbol": "btcusd",
            "side": order["side"],
            "is_live": order["is_live"],
            "is_cancelled": order["is_cancelled"],
            "executed_amount": str(order["executed"]),
            "original_amount": str(order["original"]),
            "price": order["price"],
            "timestampms": 1727179200000,
        }
        if trades:
            result["trades"] = list(order["trades"])
        return result


KEY, SECRET = "sandbox-key-000000", "sandbox-secret-000000"


@pytest.mark.asyncio
async def test_the_gemini_lifecycle_passes_and_leaves_nothing_resting() -> None:
    venue = FakeGeminiSandbox()
    report = await gemini_sandbox_lifecycle.lifecycle(
        KEY, SECRET, inner=httpx.MockTransport(venue.handler)
    )

    results = {step["step"]: step for step in report.steps}
    assert report.outcome == "pass", report.steps
    assert results["market_order"]["accepted"] is True
    assert results["recovery_by_client_order_id"]["result"] == "pass"
    assert results["undersized_rejection"]["reason"] == "InvalidQuantity"
    assert results["partial_fill"] == {
        "step": "partial_fill",
        "result": "pass",
        "status": "partially_filled",
        "fills": 1,
    }
    assert results["trading_loop"]["status"] == "submitted"
    assert results["trading_loop"]["lineage_gaps"] == []
    assert venue.live() == []
    assert report.requests_made <= gemini_sandbox_lifecycle.REQUEST_BUDGET
    text = report.render()
    assert KEY not in text and SECRET not in text and "8000000001" not in text
    assert venue.seen_keys == {KEY}


@pytest.mark.asyncio
async def test_a_sandbox_without_market_orders_is_reported_for_review() -> None:
    venue = FakeGeminiSandbox(market_orders=False, ask_depth="5")
    report = await gemini_sandbox_lifecycle.lifecycle(
        KEY, SECRET, inner=httpx.MockTransport(venue.handler)
    )

    results = {step["step"]: step for step in report.steps}
    assert results["market_order"] == {
        "step": "market_order",
        "result": "answered",
        "accepted": False,
        "reason": "InvalidOrderType",
    }
    assert results["partial_fill"]["result"] == "skipped"  # the best ask is too deep
    assert results["trading_loop"]["result"] == "refused"
    assert results["trading_loop"]["status"] == "rejected"
    assert report.outcome == "needs review"
    assert any("price collar" in note for note in report.notes)
    assert venue.live() == []


@pytest.mark.asyncio
async def test_a_failure_mid_run_still_cancels_the_resting_order() -> None:
    venue = FakeGeminiSandbox(fail_first_cancel=True)
    report = await gemini_sandbox_lifecycle.lifecycle(
        KEY, SECRET, inner=httpx.MockTransport(venue.handler)
    )

    results = {step["step"]: step for step in report.steps}
    assert results["stopped"] == {
        "step": "stopped",
        "result": "fail",
        "error": "ProviderHTTPError (HTTP 500)",
    }
    assert results["cleanup"] == {"step": "cleanup", "result": "pass", "canceled": 1}
    assert venue.live() == []
    assert report.outcome == "needs review"


class UnhelpfulSandbox(FakeGeminiSandbox):
    """Unreadable market data, and cancels that always fail."""

    def public(self, path: str) -> httpx.Response:
        if path in {"/v1/book/btcusd", "/v2/candles/btcusd/5m"}:
            return httpx.Response(200, json={"unexpected": True})
        return super().public(path)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/order/cancel":
            return httpx.Response(500, json={"result": "error", "reason": "System"})
        return super().handler(request)


@pytest.mark.asyncio
async def test_an_order_that_cannot_be_canceled_is_reported_for_manual_cleanup() -> None:
    venue = UnhelpfulSandbox()
    report = await gemini_sandbox_lifecycle.lifecycle(
        KEY, SECRET, inner=httpx.MockTransport(venue.handler)
    )

    results = {step["step"]: step for step in report.steps}
    assert results["cleanup"] == {"step": "cleanup", "result": "fail", "still_resting": 1}
    assert any("Gemini Sandbox website" in note for note in report.notes)
    assert report.outcome == "needs review"


@pytest.mark.asyncio
async def test_unreadable_book_and_candles_skip_their_steps() -> None:
    class NoMarketData(FakeGeminiSandbox):
        def public(self, path: str) -> httpx.Response:
            if path in {"/v1/book/btcusd", "/v2/candles/btcusd/5m"}:
                return httpx.Response(200, json={"unexpected": True})
            return super().public(path)

    venue = NoMarketData()
    report = await gemini_sandbox_lifecycle.lifecycle(
        KEY, SECRET, inner=httpx.MockTransport(venue.handler)
    )

    results = {step["step"]: step for step in report.steps}
    assert results["partial_fill"]["reason"] == "the Sandbox order book had no readable ask"
    assert results["trading_loop"]["reason"] == "the Sandbox returned too few closed bars"
    assert venue.live() == []


@pytest.mark.asyncio
async def test_the_gemini_probe_reads_its_key_from_hidden_prompts(monkeypatch) -> None:
    venue = FakeGeminiSandbox()
    typed = iter([KEY, SECRET])
    monkeypatch.setattr(gemini_sandbox_lifecycle, "prompt_secret", lambda label: next(typed))
    original = gemini_sandbox_lifecycle.lifecycle

    async def through_fake(api_key, api_secret):
        return await original(api_key, api_secret, inner=httpx.MockTransport(venue.handler))

    monkeypatch.setattr(gemini_sandbox_lifecycle, "lifecycle", through_fake)
    report = await gemini_sandbox_lifecycle._prompted()
    assert report.outcome == "pass" and venue.seen_keys == {KEY}


# --- check 2: Coinbase read-only reconciliation ---------------------------------------


def key_file(tmp_path: Path, private_key: str, name: str = "organizations/o/apiKeys/k") -> Path:
    path = tmp_path / "cdp_api_key.json"
    path.write_text(json.dumps({"name": name, "privateKey": private_key}), encoding="utf-8")
    return path


def pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_only_an_ecdsa_p256_key_file_is_accepted(tmp_path) -> None:
    good = pem(ec.generate_private_key(ec.SECP256R1()))
    assert coinbase_readonly_reconcile.load_key(key_file(tmp_path, good))[1] == good.strip()
    raw_ed25519 = base64.b64encode(b"\x01" * 64).decode()
    header, *_, footer = good.strip().splitlines()
    refusals = {
        raw_ed25519: "Ed25519",
        pem(ed25519.Ed25519PrivateKey.generate()): "not ECDSA P-256",
        pem(ec.generate_private_key(ec.SECP384R1())): "not ECDSA P-256",
        f"{header}\nnot-a-key\n{footer}": "readable",
    }
    for private_key, reason in refusals.items():
        with pytest.raises(ProbeRefused, match=reason):
            coinbase_readonly_reconcile.load_key(key_file(tmp_path, private_key))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProbeRefused, match="could not be read"):
        coinbase_readonly_reconcile.load_key(broken)
    for payload in ("[]", json.dumps({"name": "only-a-name"})):
        broken.write_text(payload, encoding="utf-8")
        with pytest.raises(ProbeRefused):
            coinbase_readonly_reconcile.load_key(broken)


def coinbase_account(permissions: dict[str, bool], reads: list[dict[str, str]], **faults):
    """A production-shaped Coinbase serving key permissions and successive account reads."""

    requests: list[str] = []
    listings = iter(reads)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.coinbase.com"
        assert request.headers["authorization"].startswith("Bearer ")
        path = request.url.path.removeprefix("/api/v3/brokerage")
        requests.append(path)
        if path == "/key_permissions":
            status = faults.get("permissions_status", 200)
            return httpx.Response(status, json=permissions if status == 200 else {})
        if path == "/accounts":
            balances = next(listings, None)
            if balances is None:
                return httpx.Response(500, json={"error": "INTERNAL"})
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "currency": asset,
                            "available_balance": {"value": value, "currency": asset},
                            "hold": {"value": "0", "currency": asset},
                        }
                        for asset, value in balances.items()
                    ],
                    "has_next": False,
                    "cursor": "",
                },
            )
        raise AssertionError(path)

    return httpx.MockTransport(handler), requests


VIEW_ONLY = {"can_view": True, "can_trade": False, "can_transfer": False}
HOLDINGS = {"USD": "123.456789", "BTC": "0.98765432"}


@pytest.fixture
def ecdsa_key(tmp_path) -> Path:
    return key_file(tmp_path, pem(ec.generate_private_key(ec.SECP256R1())))


@pytest.mark.asyncio
async def test_a_view_only_key_reconciles_cleanly_and_reports_only_counts(ecdsa_key) -> None:
    transport, requests = coinbase_account(VIEW_ONLY, [HOLDINGS, HOLDINGS])
    report = await coinbase_readonly_reconcile.reconcile(ecdsa_key, inner=transport)

    assert report.outcome == "pass"
    assert requests == ["/key_permissions", "/accounts", "/accounts"]
    assert {"step": "first_read", "result": "pass", "assets": 2, "positions": 1} in report.steps
    text = report.render()
    name, private_key = coinbase_readonly_reconcile.load_key(ecdsa_key)
    for secret in (name, *HOLDINGS.values(), *private_key.splitlines()[1:-1]):
        assert secret not in text


@pytest.mark.asyncio
async def test_a_key_that_can_trade_or_transfer_is_refused_before_any_account_read(
    ecdsa_key,
) -> None:
    for permissions in (
        {"can_view": True, "can_trade": True, "can_transfer": False},
        {"can_view": True, "can_trade": False, "can_transfer": True},
    ):
        transport, requests = coinbase_account(permissions, [HOLDINGS])
        report = await coinbase_readonly_reconcile.reconcile(ecdsa_key, inner=transport)
        assert report.outcome == "refused"
        assert requests == ["/key_permissions"]


@pytest.mark.asyncio
async def test_a_changed_second_read_is_a_divergence(ecdsa_key) -> None:
    moved = dict(HOLDINGS, BTC="0.5")
    transport, _ = coinbase_account(VIEW_ONLY, [HOLDINGS, moved])
    report = await coinbase_readonly_reconcile.reconcile(ecdsa_key, inner=transport)
    assert report.outcome == "diverged"
    step = next(item for item in report.steps if item["step"] == "reconcile_second_read")
    assert step["by_kind"] == {"balance": 1, "position": 1}
    assert "0.5" not in report.render()


@pytest.mark.asyncio
async def test_rejected_keys_and_failed_reads_are_reported_as_failures(ecdsa_key) -> None:
    transport, _ = coinbase_account(VIEW_ONLY, [HOLDINGS], permissions_status=401)
    rejected = await coinbase_readonly_reconcile.reconcile(ecdsa_key, inner=transport)
    assert rejected.outcome == "failed"
    assert any("open item 3" in note for note in rejected.notes)

    transport, _ = coinbase_account(VIEW_ONLY, [HOLDINGS])  # the second read fails
    unavailable = await coinbase_readonly_reconcile.reconcile(ecdsa_key, inner=transport)
    assert unavailable.outcome == "failed"
    assert unavailable.steps[-1]["step"] == "reconcile_second_read"


def test_the_key_file_path_is_asked_for_when_not_given(tmp_path, monkeypatch, ecdsa_key) -> None:
    assert coinbase_readonly_reconcile._key_path(["probe", f'"{ecdsa_key}"']) == ecdsa_key
    monkeypatch.setattr("builtins.input", lambda _: str(ecdsa_key))
    assert coinbase_readonly_reconcile._key_path(["probe"]) == ecdsa_key
    with pytest.raises(ProbeRefused, match="no file"):
        coinbase_readonly_reconcile._key_path(["probe", str(tmp_path / "missing.json")])
