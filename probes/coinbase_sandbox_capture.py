"""Capture the Coinbase Advanced Trade sandbox's responses as test fixtures.

The sandbox needs no account and no key: it returns static, predefined responses
for the Accounts and Orders endpoints, and the ``X-Sandbox`` header selects the
documented error variants. Nothing here can reach an account or move funds.

Run from the repository root::

    python -m probes.coinbase_sandbox_capture

Each response is written verbatim to ``tests/fixtures/coinbase/sandbox/`` with the
request that produced it, plus a ``manifest.json`` recording when and where.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from probes.common import ProbeReport, bounded_client, run_probe

SANDBOX_HOST = "api-sandbox.coinbase.com"
SANDBOX_URL = f"https://{SANDBOX_HOST}/api/v3/brokerage"
DOCS_URL = "https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox"
REQUEST_BUDGET = 15
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "coinbase" / "sandbox"


@dataclass(frozen=True)
class Captured:
    name: str
    method: str
    path: str
    status: int
    response: Any
    sandbox_variant: str | None = None
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None

    def to_fixture(self) -> dict[str, Any]:
        request: dict[str, Any] = {"method": self.method, "path": self.path}
        if self.sandbox_variant:
            request["x_sandbox"] = self.sandbox_variant
        if self.params:
            request["params"] = self.params
        if self.body:
            request["body"] = self.body
        return {"request": request, "status": self.status, "response": self.response}


class SandboxSession:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.captured: list[Captured] = []

    async def call(
        self,
        name: str,
        method: str,
        path: str,
        *,
        sandbox_variant: str | None = None,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"X-Sandbox": sandbox_variant} if sandbox_variant else {}
        response = await self.client.request(
            method, f"{SANDBOX_URL}{path}", headers=headers, params=params, json=body
        )
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"text": response.text}
        self.captured.append(
            Captured(
                name, method, path, response.status_code, payload, sandbox_variant, params, body
            )
        )
        return payload


async def capture(
    out_dir: Path = FIXTURES, *, inner: httpx.AsyncBaseTransport | None = None
) -> ProbeReport:
    report = ProbeReport(probe="coinbase-sandbox-capture", target=SANDBOX_URL)
    client, transport = bounded_client(hosts={SANDBOX_HOST}, budget=REQUEST_BUDGET, inner=inner)
    session = SandboxSession(client)
    client_order_id = str(uuid4())
    order = {
        "client_order_id": client_order_id,
        "product_id": "BTC-USD",
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"base_size": "0.0001"}},
    }
    try:
        accounts = await session.call("list_accounts", "GET", "/accounts")
        account_id = _first(accounts, "accounts", "uuid")
        if account_id:
            await session.call("get_account", "GET", f"/accounts/{account_id}")
        created = await session.call("create_order", "POST", "/orders", body=order)
        await session.call(
            "create_order_insufficient_fund",
            "POST",
            "/orders",
            sandbox_variant="PostOrder_insufficient_fund",
            body=dict(order, client_order_id=str(uuid4())),
        )
        order_id = _nested(created, "success_response", "order_id")
        listed = await session.call("list_orders", "GET", "/orders/historical/batch")
        # Get Order only knows the sandbox's own static orders, not the ID Create returns.
        known_id = _first(listed, "orders", "order_id")
        if known_id:
            await session.call("get_order", "GET", f"/orders/historical/{known_id}")
        await session.call(
            "list_fills",
            "GET",
            "/orders/historical/fills",
            params={"order_ids": [order_id]} if order_id else None,
        )
        cancel = {"order_ids": [order_id or "sandbox-order"]}
        await session.call("cancel_orders", "POST", "/orders/batch_cancel", body=cancel)
        await session.call(
            "cancel_orders_failure",
            "POST",
            "/orders/batch_cancel",
            sandbox_variant="CancelOrders_failure",
            body=cancel,
        )
        edit = {"order_id": order_id or "sandbox-order", "size": "0.0002", "price": "50000"}
        await session.call("edit_order", "POST", "/orders/edit", body=edit)
        await session.call(
            "edit_order_failure",
            "POST",
            "/orders/edit",
            sandbox_variant="EditOrder_failure",
            body=edit,
        )
    finally:
        await client.aclose()
        report.count_requests(transport)

    out_dir.mkdir(parents=True, exist_ok=True)
    for item in session.captured:
        (out_dir / f"{item.name}.json").write_text(
            json.dumps(item.to_fixture(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        ok = 200 <= item.status < 300
        report.step(item.name, "captured" if ok else f"HTTP {item.status}", status=item.status)
    manifest = {
        "captured_at": report.started_at.isoformat(),
        "source": SANDBOX_URL,
        "documentation": DOCS_URL,
        "authentication": "none: the sandbox is unauthenticated",
        "redaction": "none needed: responses are the sandbox's static, predefined data",
        "requests": [item.name for item in session.captured],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Error variants are expected to fail; every plain request must succeed.
    plain = [item for item in session.captured if not item.sandbox_variant]
    complete = len(session.captured) == 11 and all(200 <= item.status < 300 for item in plain)
    report.outcome = "pass" if complete else "incomplete"
    report.notes.append(f"{len(session.captured)} responses written to {out_dir.name}/")
    return report


def _nested(payload: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def _first(payload: Any, key: str, field_name: str) -> Any:
    rows = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0].get(field_name)
    return None


if __name__ == "__main__":  # pragma: no cover - the owner runs this from a terminal
    raise SystemExit(run_probe(capture))
