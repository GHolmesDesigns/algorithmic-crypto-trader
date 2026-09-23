from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from core.models import Candle, Quote
from data.backfill import HistoricalCandleBackfiller
from data.coinbase import CoinbaseRESTClient, normalize_coinbase_candle
from data.replay import JsonlReplayRecorder
from data.storage import InMemoryCandleStore
from data.stream import CoinbaseWebSocketIngestor, StaleMarketData
from data.validation import MarketDataValidationError, MarketDataValidator

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_candle(minute: int, close: str = "100") -> Candle:
    opened = NOW + timedelta(minutes=minute)
    return Candle(
        symbol="BTC-USD",
        interval="ONE_MINUTE",
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        open=Decimal("99"),
        high=Decimal("101"),
        low=Decimal("98"),
        close=Decimal(close),
        volume=Decimal("1.2"),
        source="fixture",
        as_of=opened + timedelta(minutes=1),
        ingested_at=NOW,
    )


def test_validator_rejects_duplicates_gaps_and_suspicious_outliers() -> None:
    validator = MarketDataValidator()
    with pytest.raises(MarketDataValidationError, match="duplicate"):
        validator.validate_candles(
            [make_candle(0), make_candle(0)], expected_interval=timedelta(minutes=1)
        )
    with pytest.raises(MarketDataValidationError, match="gap"):
        validator.validate_candles(
            [make_candle(0), make_candle(2)], expected_interval=timedelta(minutes=1)
        )
    with pytest.raises(MarketDataValidationError, match="outlier"):
        validator.validate_candles(
            [
                make_candle(0, "100"),
                make_candle(1).model_copy(
                    update={
                        "open": Decimal("130"),
                        "high": Decimal("131"),
                        "close": Decimal("130"),
                    }
                ),
            ],
            expected_interval=timedelta(minutes=1),
        )


def test_validator_rejects_stale_quotes() -> None:
    quote = Quote(
        symbol="BTC-USD",
        bid=Decimal("99"),
        ask=Decimal("100"),
        as_of=NOW,
        source="fixture",
        received_at=NOW,
    )
    with pytest.raises(MarketDataValidationError, match="stale"):
        MarketDataValidator(max_quote_age_seconds=30).validate_quote(
            quote, now=NOW + timedelta(seconds=31)
        )


def test_in_memory_store_is_idempotent() -> None:
    store = InMemoryCandleStore()
    candles = (make_candle(0), make_candle(1))
    assert store.upsert_many(candles) == 2
    assert store.upsert_many(candles) == 0
    assert store.latest_opened_at("BTC-USD", "ONE_MINUTE") == candles[-1].opened_at


@pytest.mark.asyncio
async def test_coinbase_rest_client_normalizes_public_candles() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/products/BTC-USD/candles")
        return httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "start": str(int(NOW.timestamp())),
                        "end": str(int((NOW + timedelta(minutes=1)).timestamp())),
                        "open": "99",
                        "high": "101",
                        "low": "98",
                        "close": "100",
                        "volume": "1.2",
                    }
                ]
            },
            request=request,
        )

    client = CoinbaseRESTClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        candles = await client.get_candles(
            "BTC-USD", NOW, NOW + timedelta(minutes=1), granularity="ONE_MINUTE"
        )
    finally:
        await client._client.aclose()
    assert candles[0].source == "coinbase-advanced-trade"
    assert candles[0].open == Decimal("99")


@pytest.mark.asyncio
async def test_backfill_resumes_from_latest_and_writes_once() -> None:
    class FakeClient:
        calls: list[tuple[datetime, datetime]] = []

        async def get_candles(self, product_id: str, start: datetime, end: datetime, **_: object):
            self.calls.append((start, end))
            return tuple(make_candle(index) for index in range(3))

    store = InMemoryCandleStore()
    client = FakeClient()
    backfiller = HistoricalCandleBackfiller(client, store)
    end = NOW + timedelta(minutes=3)
    assert await backfiller.run("BTC-USD", NOW, end) == 3
    assert await backfiller.run("BTC-USD", NOW, end) == 0
    assert len(client.calls) == 1


class FakeTransport:
    def __init__(self, messages: list[str]) -> None:
        self.messages = iter(messages)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        try:
            return next(self.messages)
        except StopIteration as exc:
            raise RuntimeError("forced disconnect") from exc

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_websocket_reconnects_gap_fills_and_records_raw_stream(tmp_path) -> None:
    heartbeat = json.dumps({"channel": "heartbeats", "events": []})
    transport = FakeTransport([heartbeat])
    gap_fills: list[tuple[str, datetime | None]] = []

    async def connect(_: str) -> FakeTransport:
        return transport

    async def gap_fill(symbol: str, start: datetime | None, end: datetime) -> None:
        gap_fills.append((symbol, start))
        stop.set()

    stop = asyncio.Event()
    recorder = JsonlReplayRecorder(tmp_path / "raw.jsonl")
    ingestor = CoinbaseWebSocketIngestor(
        ("BTC-USD",),
        connect=connect,
        gap_fill=gap_fill,
        recorder=recorder,
        reconnect_base_seconds=0.001,
    )
    await ingestor.run(stop, max_connections=2)
    assert gap_fills == [("BTC-USD", None)]
    assert recorder.read()[0].stream == "coinbase.websocket"
    assert transport.sent[0] == '{"type":"subscribe","channel":"heartbeats"}'
    assert transport.closed


def test_stream_fails_closed_without_fresh_quote() -> None:
    ingestor = CoinbaseWebSocketIngestor(("BTC-USD",), heartbeat_timeout_seconds=1)
    with pytest.raises(StaleMarketData):
        ingestor.require_fresh_quote("BTC-USD", now=NOW)


def test_normalize_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        normalize_coinbase_candle(
            "BTC-USD",
            {
                "start": str(int(NOW.timestamp())),
                "open": "99",
                "high": "101",
                "low": "98",
                "close": "100",
                "volume": "-1",
            },
            interval="ONE_MINUTE",
            received_at=NOW,
        )
