"""Bounded, provider-free research jobs for the authenticated operator surface.

The workspace deliberately owns no broker, database, or network client.  It runs
the existing pure backtester and the existing simulator-backed replay runner in
short-lived worker threads, using only approved candle fixtures.  The job store
is intentionally in memory: research is a reproducible record for the current
operator session, not a trading or operational ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, cast
from uuid import uuid4

from app.replay import ReplayRunner
from core.models import Candle
from risk.engine import ExchangeConstraints
from strategy.backtest import (
    BacktestConfig,
    Backtester,
    BacktestReport,
    CostAssumptions,
    WalkForwardConfig,
    WalkForwardReport,
    run_walk_forward,
)
from strategy.reference import AlwaysBuyStrategy, MovingAverageCrossStrategy

MAX_CANDLES = 5000
MAX_ACTIVE_JOBS = 2
MAX_STORED_JOBS = 24
MAX_WALK_FORWARD_WINDOWS = 24
MAX_JOB_SECONDS = 30
MAX_EXPORT_BYTES = 250_000


class ResearchJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class ResearchRunRequest:
    """Validated request accepted by both the JSON and HTML routes."""

    def __init__(
        self,
        *,
        dataset_id: str,
        strategy_version: str,
        run_type: Literal["backtest", "replay"] = "backtest",
        initial_cash: Decimal = Decimal("100000"),
        maker_fee_rate: Decimal = Decimal("0"),
        taker_fee_rate: Decimal = Decimal("0"),
        spread_bps: Decimal = Decimal("0"),
        slippage_bps: Decimal = Decimal("0"),
        fee_asset: str = "USD",
        partial_fill_ratio: Decimal = Decimal("1"),
        train_bars: int = 16,
        test_bars: int = 16,
        step_bars: int | None = None,
        holdout_bars: int = 0,
        quantity: Decimal = Decimal("1"),
    ) -> None:
        if not dataset_id or len(dataset_id) > 64:
            raise ValueError("dataset_id is required and must be at most 64 characters")
        if strategy_version not in STRATEGY_VERSIONS:
            raise ValueError("strategy_version is not an approved strategy")
        if run_type not in {"backtest", "replay"}:
            raise ValueError("run_type must be backtest or replay")
        if initial_cash <= 0 or initial_cash > Decimal("1000000000"):
            raise ValueError("initial_cash must be between 0 and 1000000000")
        if any(value < 0 for value in (maker_fee_rate, taker_fee_rate, spread_bps, slippage_bps)):
            raise ValueError("cost assumptions cannot be negative")
        if fee_asset.strip() == "" or len(fee_asset) > 16:
            raise ValueError("fee_asset is required and must be at most 16 characters")
        if partial_fill_ratio <= 0 or partial_fill_ratio > 1:
            raise ValueError("partial_fill_ratio must be greater than 0 and at most 1")
        if quantity <= 0 or quantity > 1000:
            raise ValueError("quantity must be greater than 0 and at most 1000")
        if train_bars <= 0 or train_bars > MAX_CANDLES:
            raise ValueError(f"train_bars must be between 1 and {MAX_CANDLES}")
        if test_bars <= 0 or test_bars > MAX_CANDLES:
            raise ValueError(f"test_bars must be between 1 and {MAX_CANDLES}")
        if step_bars is not None and (step_bars <= 0 or step_bars > MAX_CANDLES):
            raise ValueError(f"step_bars must be between 1 and {MAX_CANDLES}")
        if holdout_bars < 0 or holdout_bars > MAX_CANDLES:
            raise ValueError(f"holdout_bars must be between 0 and {MAX_CANDLES}")
        self.dataset_id = dataset_id
        self.strategy_version = strategy_version
        self.run_type = run_type
        self.initial_cash = initial_cash
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.spread_bps = spread_bps
        self.slippage_bps = slippage_bps
        self.fee_asset = fee_asset.strip()
        self.partial_fill_ratio = partial_fill_ratio
        self.train_bars = train_bars
        self.test_bars = test_bars
        self.step_bars = step_bars
        self.holdout_bars = holdout_bars
        self.quantity = quantity

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ResearchRunRequest:
        def text(name: str, default: str = "") -> str:
            value = values.get(name, default)
            return str(value).strip()

        def decimal(name: str, default: str) -> Decimal:
            raw = values.get(name, default)
            try:
                return Decimal(str(raw).strip())
            except (ArithmeticError, ValueError) as exc:
                raise ValueError(f"{name} must be a decimal number") from exc

        def integer(name: str, default: int, *, optional: bool = False) -> int | None:
            raw = values.get(name, "" if optional else default)
            if optional and (raw is None or str(raw).strip() == ""):
                return None
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a whole number") from exc

        raw_run_type = text("run_type", "backtest") or "backtest"
        if raw_run_type not in {"backtest", "replay"}:
            raise ValueError("run_type must be backtest or replay")
        run_type = cast(Literal["backtest", "replay"], raw_run_type)
        return cls(
            dataset_id=text("dataset_id"),
            strategy_version=text("strategy_version"),
            run_type=run_type,
            initial_cash=decimal("initial_cash", "100000"),
            maker_fee_rate=decimal("maker_fee_rate", "0"),
            taker_fee_rate=decimal("taker_fee_rate", "0"),
            spread_bps=decimal("spread_bps", "0"),
            slippage_bps=decimal("slippage_bps", "0"),
            fee_asset=text("fee_asset", "USD") or "USD",
            partial_fill_ratio=decimal("partial_fill_ratio", "1"),
            train_bars=integer("train_bars", 16) or 16,
            test_bars=integer("test_bars", 16) or 16,
            step_bars=integer("step_bars", 0, optional=True),
            holdout_bars=integer("holdout_bars", 0) or 0,
            quantity=decimal("quantity", "1"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "strategy_version": self.strategy_version,
            "run_type": self.run_type,
            "initial_cash": str(self.initial_cash),
            "maker_fee_rate": str(self.maker_fee_rate),
            "taker_fee_rate": str(self.taker_fee_rate),
            "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps),
            "fee_asset": self.fee_asset,
            "partial_fill_ratio": str(self.partial_fill_ratio),
            "train_bars": self.train_bars,
            "test_bars": self.test_bars,
            "step_bars": self.step_bars,
            "holdout_bars": self.holdout_bars,
            "quantity": str(self.quantity),
        }


@dataclass(frozen=True, slots=True)
class ApprovedDataset:
    dataset_id: str
    name: str
    description: str
    symbol: str
    interval: str
    source: str
    approval: str
    approved_at: str
    candles: tuple[Candle, ...]
    digest: str

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.dataset_id,
            "name": self.name,
            "description": self.description,
            "symbol": self.symbol,
            "interval": self.interval,
            "source": self.source,
            "approval": self.approval,
            "approved_at": self.approved_at,
            "candle_count": len(self.candles),
            "window_start": self.candles[0].opened_at.isoformat(),
            "window_end": self.candles[-1].closed_at.isoformat(),
            "digest": self.digest,
        }


@dataclass(slots=True)
class ResearchJob:
    run_id: str
    request: ResearchRunRequest
    status: ResearchJobStatus = ResearchJobStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "request": self.request.to_dict(),
            "strategy_version": self.request.strategy_version,
            "dataset_id": self.request.dataset_id,
            "run_type": self.request.run_type,
            "error": self.error,
            "result": self.result,
        }


STRATEGY_VERSIONS: dict[str, str] = {
    "always-buy-v1": "Known-answer buy-once benchmark",
    "ma-cross-3-8-v1": "Moving-average crossover reference strategy",
}


def _fixture_dataset(
    dataset_id: str,
    name: str,
    description: str,
    closes: Sequence[int],
) -> ApprovedDataset:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index, close_value in enumerate(closes):
        opened = start + timedelta(hours=index)
        closed = opened + timedelta(hours=1)
        close = Decimal(close_value)
        previous = Decimal(closes[index - 1]) if index else close
        high = max(close, previous) + Decimal("1")
        low = min(close, previous) - Decimal("1")
        candles.append(
            Candle(
                symbol="BTC-USD",
                interval="ONE_HOUR",
                opened_at=opened,
                closed_at=closed,
                open=previous,
                high=high,
                low=max(low, Decimal("1")),
                close=close,
                volume=Decimal("1"),
                source="approved-check-in-fixture",
                as_of=closed,
                ingested_at=closed,
            )
        )
    canonical = json.dumps(
        [
            {
                "opened_at": candle.opened_at.isoformat(),
                "open": str(candle.open),
                "high": str(candle.high),
                "low": str(candle.low),
                "close": str(candle.close),
                "volume": str(candle.volume),
            }
            for candle in candles
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return ApprovedDataset(
        dataset_id=dataset_id,
        name=name,
        description=description,
        symbol="BTC-USD",
        interval="ONE_HOUR",
        source="checked-in deterministic fixture",
        approval="repository-approved research fixture",
        approved_at="2026-09-27",
        candles=tuple(candles),
        digest=digest,
    )


def approved_datasets() -> tuple[ApprovedDataset, ...]:
    """Return the small, reviewable datasets exposed by the workspace."""

    return (
        _fixture_dataset(
            "btc-usd-calm-trend-v1",
            "BTC-USD calm trend",
            "A deterministic rising and ranging window for known-answer comparisons.",
            [100, 101, 102, 103, 104, 106, 108, 107, 109, 111, 112, 113, 115, 116, 117, 118]
            + [118, 117, 119, 120, 121, 123, 122, 124, 125, 126, 128, 127, 129, 130, 132, 131]
            + [133, 134, 132, 135, 136, 138, 137, 139, 141, 140, 142, 144, 143, 145, 147, 146]
            + [148, 149, 151, 150, 152, 154, 153, 155, 156, 158, 157, 159, 161, 160, 162, 164],
        ),
        _fixture_dataset(
            "btc-usd-high-volatility-v1",
            "BTC-USD high volatility",
            "A deterministic alternating window for stress-testing costs and refusals.",
            [100, 108, 96, 112, 91, 118, 88, 121, 94, 126, 89, 116, 98, 130, 92, 124]
            + [86, 132, 90, 138, 95, 128, 87, 141, 93, 135, 89, 145, 97, 139, 91, 148]
            + [100, 142, 96, 151, 93, 146, 99, 155, 95, 149, 101, 158, 98, 152, 104, 160]
            + [100, 154, 97, 162, 102, 157, 99, 166, 104, 161, 107, 170, 105, 165, 110, 172],
        ),
    )


def _strategy(version: str, quantity: Decimal):
    if version == "always-buy-v1":
        return AlwaysBuyStrategy(quantity)
    if version == "ma-cross-3-8-v1":
        return MovingAverageCrossStrategy(3, 8, quantity)
    raise ValueError("strategy_version is not an approved strategy")


def _report_summary(report: BacktestReport) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["max_drawdown_duration"] = str(report.max_drawdown_duration)
    return payload


def _walk_forward_summary(report: WalkForwardReport) -> dict[str, Any]:
    return {
        "windows": [
            {
                "train_start": window.train_start.isoformat(),
                "train_end": window.train_end.isoformat(),
                "test_start": window.test_start.isoformat(),
                "test_end": window.test_end.isoformat(),
                "report": _report_summary(window.report),
            }
            for window in report.windows
        ],
        "holdout": _report_summary(report.holdout) if report.holdout else None,
    }


def _replay_summary(result: Any, report: BacktestReport) -> dict[str, Any]:
    return {
        "signal_count": len(result.signals),
        "fill_count": len(result.fills),
        "refusal_count": len(result.refusals),
        "final_equity": str(result.final_equity),
        "backtest_final_equity": str(report.final_equity),
        "parity_delta": str(result.final_equity - report.final_equity),
        "refusals": [
            {
                "status": item.status.value,
                "detail": item.detail,
                "failed_gate": item.approval.failed_gate if item.approval else None,
            }
            for item in result.refusals[:25]
        ],
    }


class ResearchWorkspace:
    """Own bounded research jobs without access to a broker or provider client."""

    def __init__(
        self,
        datasets: Sequence[ApprovedDataset] | None = None,
        *,
        runner: Callable[[ResearchRunRequest, ApprovedDataset], dict[str, Any]] | None = None,
    ) -> None:
        self.datasets = {item.dataset_id: item for item in (datasets or approved_datasets())}
        self.jobs: OrderedDict[str, ResearchJob] = OrderedDict()
        self._semaphore = asyncio.Semaphore(MAX_ACTIVE_JOBS)
        self._runner = runner or self._run_sync

    def catalog(self) -> list[dict[str, Any]]:
        return [dataset.summary() for dataset in self.datasets.values()]

    def strategies(self) -> list[dict[str, str]]:
        return [{"version": version, "name": name} for version, name in STRATEGY_VERSIONS.items()]

    async def submit(self, request: ResearchRunRequest) -> ResearchJob:
        dataset = self.datasets.get(request.dataset_id)
        if dataset is None:
            raise ValueError("dataset_id is not an approved dataset")
        if len(dataset.candles) > MAX_CANDLES:
            raise ValueError(f"approved dataset exceeds the {MAX_CANDLES}-candle limit")
        available = len(dataset.candles) - request.holdout_bars
        if available < request.train_bars + request.test_bars:
            raise ValueError("the selected dataset is too short for these walk-forward windows")
        step = request.step_bars or request.test_bars
        windows = 1 + (available - request.train_bars - request.test_bars) // step
        if windows > MAX_WALK_FORWARD_WINDOWS:
            raise ValueError(
                f"walk-forward configuration exceeds {MAX_WALK_FORWARD_WINDOWS} windows"
            )
        active = sum(
            job.status in {ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING}
            for job in self.jobs.values()
        )
        if active >= MAX_ACTIVE_JOBS:
            raise ValueError("the research queue is full; wait for a run to finish")
        job = ResearchJob(run_id=str(uuid4()), request=request)
        self.jobs[job.run_id] = job
        while len(self.jobs) > MAX_STORED_JOBS:
            old_id, old = next(iter(self.jobs.items()))
            if old.status in {ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING}:
                break
            self.jobs.pop(old_id)
        job.task = asyncio.create_task(self._run(job, dataset))
        return job

    async def _run(self, job: ResearchJob, dataset: ApprovedDataset) -> None:
        job.status = ResearchJobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        try:
            async with self._semaphore:
                job.result = await asyncio.wait_for(
                    asyncio.to_thread(self._runner, job.request, dataset),
                    timeout=MAX_JOB_SECONDS,
                )
            job.status = ResearchJobStatus.COMPLETE
        except Exception as exc:
            job.status = ResearchJobStatus.FAILED
            job.error = str(exc)[:500]
        finally:
            job.finished_at = datetime.now(UTC)

    async def wait(self, run_id: str) -> ResearchJob:
        job = self.jobs.get(run_id)
        if job is None:
            raise KeyError(run_id)
        if job.task is not None:
            await job.task
        return job

    def get(self, run_id: str) -> ResearchJob | None:
        return self.jobs.get(run_id)

    def compare(self, run_ids: Sequence[str]) -> list[dict[str, Any]]:
        result = []
        for run_id in run_ids[:5]:
            job = self.jobs.get(run_id)
            if job is None or job.status is not ResearchJobStatus.COMPLETE or job.result is None:
                continue
            report = job.result["report"]
            result.append(
                {
                    "run_id": run_id,
                    "strategy_version": job.request.strategy_version,
                    "dataset_id": job.request.dataset_id,
                    "run_type": job.request.run_type,
                    "final_equity": report["final_equity"],
                    "total_return_pct": report["total_return_pct"],
                    "max_drawdown": report["max_drawdown"],
                    "trade_count": report["trade_count"],
                    "exposure": report["exposure"],
                }
            )
        return result

    def export(self, run_id: str) -> bytes:
        job = self.jobs.get(run_id)
        if job is None:
            raise KeyError(run_id)
        if job.status is not ResearchJobStatus.COMPLETE or job.result is None:
            raise ValueError("only a completed run can be exported")
        result = {
            "record_type": "research-run",
            "disclaimer": "Past results are not a promise of profit.",
            "run_id": job.run_id,
            "created_at": job.created_at.isoformat(),
            "request": job.request.to_dict(),
            "dataset": self.datasets[job.request.dataset_id].summary(),
            "report": job.result["report"],
            "walk_forward": job.result["walk_forward"],
            "replay": job.result.get("replay"),
            "redaction": (
                "Sensitive connection data, recipients, and infrastructure identifiers "
                "are excluded."
            ),
        }
        payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
        if len(payload) > MAX_EXPORT_BYTES:
            raise ValueError("the redacted export exceeds its size limit")
        return payload

    def _run_sync(self, request: ResearchRunRequest, dataset: ApprovedDataset) -> dict[str, Any]:
        costs = CostAssumptions(
            maker_fee_rate=request.maker_fee_rate,
            taker_fee_rate=request.taker_fee_rate,
            spread_bps=request.spread_bps,
            slippage_bps=request.slippage_bps,
            fee_asset=request.fee_asset,
            partial_fill_ratio=request.partial_fill_ratio,
        )
        config = BacktestConfig(
            initial_cash=request.initial_cash,
            data_source=dataset.source,
            granularity=dataset.interval,
            quote_asset="USD",
            costs=costs,
        )
        report = Backtester(config).run(
            dataset.candles, _strategy(request.strategy_version, request.quantity)
        )
        walk_forward = run_walk_forward(
            dataset.candles,
            lambda _training: _strategy(request.strategy_version, request.quantity),
            WalkForwardConfig(
                train_bars=request.train_bars,
                test_bars=request.test_bars,
                step_bars=request.step_bars,
                holdout_bars=request.holdout_bars,
            ),
            backtest_config=config,
        )
        replay = None
        if request.run_type == "replay":
            if request.partial_fill_ratio != 1:
                raise ValueError("replay requires a full-fill ratio of 1")
            replay_result = asyncio.run(
                ReplayRunner(
                    config=config,
                    constraints=ExchangeConstraints(
                        min_quantity=Decimal("0.001"),
                        max_quantity=Decimal("1000"),
                        quantity_increment=Decimal("0.001"),
                        min_notional=Decimal("10"),
                        max_notional=Decimal("100000"),
                        price_increment=Decimal("0.01"),
                    ),
                ).run(dataset.candles, _strategy(request.strategy_version, request.quantity))
            )
            replay = _replay_summary(replay_result, report)
        return {
            "disclaimer": "Past results are not a promise of profit.",
            "dataset": dataset.summary(),
            "report": _report_summary(report),
            "walk_forward": _walk_forward_summary(walk_forward),
            "replay": replay,
        }

    async def close(self) -> None:
        for job in self.jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in self.jobs.values() if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
