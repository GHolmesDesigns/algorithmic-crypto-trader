"""Phase 1 gate: a forced WebSocket disconnect is recovered with gap fill and no data loss.

The Advanced Trade candles channel sends five-minute buckets updated every second.
Only closed buckets may enter the candle store; the bucket in progress at a
disconnect is dropped and later written from REST once it has closed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from data.backfill import HistoricalCandleBackfiller
from data.coinbase import CoinbaseRESTClient
from data.replay import JsonlReplayRecorder
from data.storage import InMemoryCandleStore
from data.stream import CoinbaseWebSocketIngestor
from data.validation import MarketDataValidator

BUCKET = timedelta(minutes=5)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def bucket(k: int) -> datetime:
    return T0 + BUCKET * k


def ohlcv(k: int, *, close: str | None = None) -> dict[str, str]:
    base = Decimal(60000 + 10 * k)
    final = Decimal(close) if close is not None else base + 5
    return {
        "start": str(int(bucket(k).timestamp())),
        "open": str(base),
        "high": str(max(base, final) + 3),
        "low": str(min(base, final) - 3),
        "close": str(final),
        "volume": "1.5",
    }


def candle_update(k: int, *, close: str | None = None) -> str:
    raw = dict(ohlcv(k, close=close), product_id="BTC-USD")
    return json.dumps(
        {"channel": "candles", "events": [{"type": "update", "candles": [raw]}]},
        separators=(",", ":"),
    )


HEARTBEAT = json.dumps({"channel": "heartbeats", "events": [{"heartbeat_counter": 1}]})


class ScriptedSocket:
    """Replays messages, then drops the connection or ends the session."""

    def __init__(self, messages: list[str], *, then: str, stop: asyncio.Event) -> None:
        self.messages = list(messages)
        self.then = then
        self.stop = stop
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if self.then == "drop":
            raise ConnectionResetError("forced disconnect")
        self.stop.set()
        return HEARTBEAT

    async def close(self) -> None:
        self.closed = True


def rest_candles(requests: list[tuple[datetime, datetime]]):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/products/BTC-USD/candles"
        assert request.url.params["granularity"] == "FIVE_MINUTE"
        start = datetime.fromtimestamp(int(request.url.params["start"]), tz=UTC)
        end = datetime.fromtimestamp(int(request.url.params["end"]), tz=UTC)
        requests.append((start, end))
        rows = []
        k = int((start - T0) / BUCKET)
        while bucket(k) < end:
            rows.append(ohlcv(k))
            k += 1
        # Coinbase returns newest first.
        return httpx.Response(200, json={"candles": list(reversed(rows))}, request=request)

    return handler


@pytest.mark.asyncio
async def test_forced_disconnect_is_gap_filled_with_no_unexplained_data_loss(tmp_path) -> None:
    stop = asyncio.Event()
    store = InMemoryCandleStore()
    rest_requests: list[tuple[datetime, datetime]] = []
    rest = CoinbaseRESTClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(rest_candles(rest_requests)))
    )
    backfiller = HistoricalCandleBackfiller(rest, store)
    gap_fills: list[tuple[datetime | None, datetime]] = []

    async def on_candle(candle) -> None:
        store.upsert_many((candle,))

    async def gap_fill(product_id: str, last_closed: datetime | None, end: datetime) -> None:
        gap_fills.append((last_closed, end))
        await backfiller.run(product_id, last_closed or T0, end, granularity="FIVE_MINUTE")

    first = ScriptedSocket(
        [
            HEARTBEAT,
            candle_update(0, close="60001"),
            candle_update(0),
            candle_update(1),
            candle_update(1),
            candle_update(2),
            HEARTBEAT,
            candle_update(3, close="59000"),  # bucket 3 is still in progress at the drop
        ],
        then="drop",
        stop=stop,
    )
    second = ScriptedSocket(
        [
            HEARTBEAT,
            candle_update(6),
            candle_update(2),  # a late update for a closed bucket is ignored
            candle_update(7),
            candle_update(7),
            candle_update(8),  # in progress when the session ends
        ],
        then="end",
        stop=stop,
    )
    sockets = iter((first, second))

    async def connect(_: str) -> ScriptedSocket:
        return next(sockets)

    recorder = JsonlReplayRecorder(tmp_path / "raw.jsonl")
    ingestor = CoinbaseWebSocketIngestor(
        ("BTC-USD",),
        connect=connect,
        on_candle=on_candle,
        gap_fill=gap_fill,
        recorder=recorder,
        reconnect_base_seconds=0.001,
        # The reconnect happens while bucket 6 is in progress.
        clock=lambda: bucket(6) + timedelta(seconds=40),
    )

    await ingestor.run(stop, max_connections=2)

    stored = store.read("BTC-USD", "FIVE_MINUTE")
    assert [candle.opened_at for candle in stored] == [bucket(k) for k in range(8)]
    # Continuous five-minute series: no duplicates, gaps, or bad durations.
    MarketDataValidator(max_price_change_ratio=Decimal("0.05")).validate_candles(
        stored, expected_interval=BUCKET
    )
    # The gap was filled from REST for exactly the closed buckets the stream missed.
    assert gap_fills == [(bucket(2), bucket(6))]
    assert rest_requests == [(bucket(3), bucket(6))]
    # Bucket 3's in-progress stream update was never stored as a closed bar.
    assert stored[3].close == Decimal(ohlcv(3)["close"])
    assert stored[0].close == Decimal(ohlcv(0)["close"])
    assert ingestor.last_candle_at["BTC-USD"] == bucket(7)
    assert len(recorder.read()) == 15  # every raw message, including the final heartbeat
    assert first.closed and second.closed
    await rest.close()


@pytest.mark.asyncio
async def test_disconnect_before_any_bucket_closes_backfills_from_the_window_start() -> None:
    stop = asyncio.Event()
    gap_fills: list[tuple[datetime | None, datetime]] = []

    async def gap_fill(product_id: str, last_closed: datetime | None, end: datetime) -> None:
        gap_fills.append((last_closed, end))
        stop.set()

    socket = ScriptedSocket([HEARTBEAT, candle_update(0)], then="drop", stop=stop)

    async def connect(_: str) -> ScriptedSocket:
        return socket

    emitted = []

    async def on_candle(candle) -> None:
        emitted.append(candle)

    ingestor = CoinbaseWebSocketIngestor(
        ("BTC-USD",),
        connect=connect,
        on_candle=on_candle,
        gap_fill=gap_fill,
        reconnect_base_seconds=0.001,
        clock=lambda: bucket(1) + timedelta(seconds=10),
    )
    await ingestor.run(stop, max_connections=1)

    assert emitted == []
    assert gap_fills == [(None, bucket(1))]
