"""Owner-run: reconcile a real Coinbase account through the adapter, read-only.

The key must be a View-only ECDSA key; the probe asks Coinbase what it can do
before reading anything and stops unless it can view but can neither trade nor
transfer. An Ed25519 key is refused before any request, because the adapter
signs ES256. The account is read twice and the second read is reconciled
against the first; the result records only permissions and counts.

Run from the repository root with the key file Coinbase downloaded::

    python -m probes.coinbase_readonly_reconcile "C:\\path\\to\\cdp_api_key.json"
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import httpx
from brokers.coinbase import COINBASE_REST_URL, CoinbaseBroker
from brokers.http import ProviderError
from core.resilience import CircuitOpen, RateLimitExceeded
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from portfolio.reconciliation import PortfolioState, Reconciler, ReconciliationUnavailable
from risk.kill_switch import KillSwitch

from probes.common import (
    BudgetExhausted,
    ProbeRefused,
    ProbeReport,
    UnexpectedHost,
    bounded_client,
    run_probe,
)

PRODUCTION_HOST = "api.coinbase.com"
# One permission read plus two full account listings of up to 40 pages each.
REQUEST_BUDGET = 85
VIEW_ONLY = {"can_view": True, "can_trade": False, "can_transfer": False}


def load_key(path: Path) -> tuple[str, str]:
    """Return the key name and PEM private key, refusing anything but ECDSA P-256."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProbeRefused("the key file could not be read as the JSON Coinbase downloads") from exc
    if not isinstance(payload, dict):
        raise ProbeRefused("the key file is not the JSON object Coinbase downloads")
    name = str(payload.get("name") or payload.get("id") or "").strip()
    private_key = str(payload.get("privateKey") or "").strip()
    if not name or not private_key:
        raise ProbeRefused("the key file has no key name and private key")
    if "BEGIN" not in private_key:
        raise ProbeRefused(
            "this is an Ed25519 key; the adapter signs ES256, so create an ECDSA key instead"
        )
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ProbeRefused("the private key in the file is not a readable PEM key") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ProbeRefused("the key is not ECDSA P-256; the adapter's ES256 signing needs one")
    return name, private_key


async def reconcile(
    key_file: Path, *, inner: httpx.AsyncBaseTransport | None = None
) -> ProbeReport:
    name, private_key = load_key(key_file)
    report = ProbeReport(probe="coinbase-readonly-reconcile", target=COINBASE_REST_URL)
    report.keep_out(name, private_key)
    client, transport = bounded_client(hosts={PRODUCTION_HOST}, budget=REQUEST_BUDGET, inner=inner)
    broker = CoinbaseBroker(api_key=name, private_key=private_key, client=client)
    try:
        permissions = await broker.key_permissions()
        view_only = permissions == VIEW_ONLY
        report.step("key_permissions", "pass" if view_only else "refused", **permissions)
        if not view_only:
            report.outcome = "refused"
            report.notes.append(
                "Stopped before reading the account: the key must be View-only, with trading "
                "and transfers disabled."
            )
            return report
        balances = await broker.get_balances()
        positions = await broker.get_positions()
        report.step("first_read", "pass", assets=len(balances), positions=len(positions))
        result = await Reconciler(broker, KillSwitch()).reconcile(
            PortfolioState(balances=balances, positions=positions)
        )
        kinds = Counter(item.entity_type for item in result.discrepancies)
        report.step(
            "reconcile_second_read",
            "pass" if not result.discrepancies else "diverged",
            discrepancies=len(result.discrepancies),
            by_kind=dict(sorted(kinds.items())),
        )
        report.outcome = "pass" if not result.discrepancies else "diverged"
    except ReconciliationUnavailable:
        report.step("reconcile_second_read", "fail", error="the second read failed")
        report.outcome = "failed"
    except (ProviderError, CircuitOpen, RateLimitExceeded, BudgetExhausted, UnexpectedHost) as exc:
        status = getattr(exc, "status_code", None)
        report.step("coinbase", "fail", error=type(exc).__name__, http_status=status)
        if status == 401:
            report.notes.append(
                "Coinbase rejected the key (401). If the key is ECDSA and View-only, record "
                "this against #11 open item 3: Coinbase may not accept ES256 for this account."
            )
        report.outcome = "failed"
    finally:
        await client.aclose()
        report.count_requests(transport)
        listings = sum(1 for _, path in transport.requests if path.endswith("/accounts"))
        report.notes.append(f"{listings} account-page request(s) across both reads.")
    return report


def _key_path(argv: list[str]) -> Path:
    raw = argv[1] if len(argv) > 1 else input("Path to the key file Coinbase downloaded: ")
    path = Path(raw.strip().strip('"'))
    if not path.is_file():
        raise ProbeRefused(f"no file at {path}")
    return path


if __name__ == "__main__":  # pragma: no cover - the owner runs this from a terminal
    key_path = _key_path(sys.argv)
    raise SystemExit(run_probe(lambda: reconcile(key_path)))
