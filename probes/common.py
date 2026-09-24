"""Shared rules for exchange probes: exact targets, a request budget, and redacted results.

Every probe names the hosts it may reach and the most requests it may make. The
transport refuses anything else before it leaves the machine. Results record
steps, statuses, and counts; credentials registered with ``keep_out`` can never
be written, and a result that would contain one is refused instead.
"""

from __future__ import annotations

import asyncio
import getpass
import json
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

RESULTS_DIR = Path.home() / "crypto-trader-probe-results"


class ProbeRefused(RuntimeError):
    """A safety condition failed, so the probe stopped before going further."""


class BudgetExhausted(RuntimeError):
    """The probe tried to make more requests than its fixed budget allows."""


class UnexpectedHost(RuntimeError):
    """The probe tried to reach a host outside its exact targets."""


class BoundedTransport(httpx.AsyncBaseTransport):
    """Permit only the named hosts and at most ``budget`` requests, and count them.

    ``reserve`` requests are held back for cleanup: the probe's steps can spend
    only ``budget - reserve`` until ``release_reserve`` is called in ``finally``.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        hosts: Iterable[str],
        budget: int,
        reserve: int = 0,
    ) -> None:
        if budget <= 0 or not 0 <= reserve < budget:
            raise ValueError("a probe needs a positive budget larger than its cleanup reserve")
        self.inner = inner
        self.hosts = frozenset(hosts)
        self.budget = budget
        self.limit = budget - reserve
        self.requests: list[tuple[str, str]] = []

    def release_reserve(self) -> None:
        self.limit = self.budget

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in self.hosts:
            raise UnexpectedHost(
                f"refusing a request outside the probe's targets: {request.url.host}"
            )
        if len(self.requests) >= self.limit:
            raise BudgetExhausted(f"the probe's budget of {self.limit} requests is spent")
        self.requests.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def bounded_client(
    *,
    hosts: Iterable[str],
    budget: int,
    reserve: int = 0,
    inner: httpx.AsyncBaseTransport | None = None,
) -> tuple[httpx.AsyncClient, BoundedTransport]:
    transport = BoundedTransport(
        inner or httpx.AsyncHTTPTransport(), hosts=hosts, budget=budget, reserve=reserve
    )
    return httpx.AsyncClient(transport=transport, timeout=15.0), transport


@dataclass
class ProbeReport:
    """The redacted record of one probe run."""

    probe: str
    target: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    steps: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "incomplete"
    requests_made: int = 0
    request_budget: int = 0
    notes: list[str] = field(default_factory=list)
    _secrets: list[str] = field(default_factory=list, repr=False)

    def step(self, name: str, result: str, **details: Any) -> None:
        self.steps.append({"step": name, "result": result, **details})

    def keep_out(self, *values: str) -> None:
        """Register credentials; a result containing any of them is never written."""

        for value in values:
            for piece in (value, *value.splitlines()):
                piece = piece.strip()
                if len(piece) >= 8 and "-----" not in piece:
                    self._secrets.append(piece)

    def count_requests(self, transport: BoundedTransport) -> None:
        self.requests_made = len(transport.requests)
        self.request_budget = transport.budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "target": self.target,
            "started_at": self.started_at.isoformat(),
            "outcome": self.outcome,
            "requests_made": self.requests_made,
            "request_budget": self.request_budget,
            "steps": self.steps,
            "notes": self.notes,
        }

    def render(self) -> str:
        text = json.dumps(self.to_dict(), indent=2, default=str)
        if any(secret in text for secret in self._secrets):
            raise ProbeRefused("the result would contain a credential, so it was not written")
        return text

    def write(self, directory: Path = RESULTS_DIR) -> Path:
        text = self.render()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.probe}-{self.started_at:%Y%m%dT%H%M%SZ}.json"
        path.write_text(text + "\n", encoding="utf-8")
        return path


def prompt_secret(label: str, *, ask: Callable[[str], str] = getpass.getpass) -> str:
    """Read one credential from a hidden prompt; nothing is echoed or stored."""

    value = ask(f"{label} (hidden, press Enter when done): ").strip()
    if not value:
        raise ProbeRefused(f"no {label} was entered")
    return value


def run_probe(
    probe: Callable[[], Coroutine[Any, Any, ProbeReport]], *, results: Path = RESULTS_DIR
) -> int:
    """Run a probe, print a plain summary, and save the redacted result."""

    try:
        report: ProbeReport = asyncio.run(probe())
    except (ProbeRefused, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc or 'cancelled'}")
        return 2
    path = report.write(results)
    print(f"\n{report.probe}: {report.outcome.upper()}")
    for step in report.steps:
        print(f"  - {step['step']}: {step['result']}")
    for note in report.notes:
        print(f"  note: {note}")
    print(f"\nRequests: {report.requests_made} of {report.request_budget} allowed.")
    print(f"Saved the redacted result to {path}")
    print("Tell Claude the check finished; nothing in that file is secret.")
    return 0 if report.outcome == "pass" else 1
