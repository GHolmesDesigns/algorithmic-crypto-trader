"""Coinbase public WebSocket ingestion with heartbeat and reconnect handling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from core.models import Candle, Quote

from data.coinbase import COINBASE_WS_URL, normalize_coinbase_candle
from data.replay import JsonlReplayRecorder


class WebSocketTransport(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class StaleMarketData(RuntimeError):
    """Raised when no heartbeat or quote has arrived within the freshness budget."""


class CoinbaseWebSocketIngestor:
    def __init__(
        self,
        product_ids: tuple[str, ...],
        *,
        connect: Callable[[str], Awaitable[WebSocketTransport]] | None = None,
        on_candle: Callable[[Candle], Awaitable[None]] | None = None,
        on_quote: Callable[[Quote], Awaitable[None]] | None = None,
        gap_fill: Callable[[str, datetime | None, datetime], Awaitable[None]] | None = None,
        recorder: JsonlReplayRecorder | None = None,
        heartbeat_timeout_seconds: float = 30.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        if not product_ids:
            raise ValueError("at least one Coinbase product is required")
        if heartbeat_timeout_seconds <= 0 or reconnect_base_seconds <= 0:
            raise ValueError("stream timing values must be positive")
        self.product_ids = product_ids
        self.connect = connect or self._connect_real
        self.on_candle = on_candle
        self.on_quote = on_quote
        self.gap_fill = gap_fill
        self.recorder = recorder
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.reconnect_base_seconds = reconnect_base_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.last_heartbeat: datetime | None = None
        self.last_candle_at: dict[str, datetime] = {}
        self.last_quote: dict[str, Quote] = {}

    async def _connect_real(self, url: str) -> WebSocketTransport:
        import websockets

        return await websockets.connect(url)

    async def run(self, stop: asyncio.Event, *, max_connections: int | None = None) -> None:
        attempts = 0
        delay = self.reconnect_base_seconds
        while not stop.is_set():
            if max_connections is not None and attempts >= max_connections:
                return
            attempts += 1
            transport: WebSocketTransport | None = None
            try:
                transport = await self.connect(COINBASE_WS_URL)
                await self._subscribe(transport)
                await self._consume(transport, stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.gap_fill is not None and not stop.is_set():
                    gap_end = datetime.now(UTC)
                    for product_id in self.product_ids:
                        await self.gap_fill(
                            product_id, self.last_candle_at.get(product_id), gap_end
                        )
                if stop.is_set():
                    return
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max_seconds, delay * 2)
            finally:
                if transport is not None:
                    await transport.close()

    async def _subscribe(self, transport: WebSocketTransport) -> None:
        for channel in ("heartbeats", "candles", "ticker"):
            payload: dict[str, Any] = {"type": "subscribe", "channel": channel}
            if channel != "heartbeats":
                payload["product_ids"] = list(self.product_ids)
            await transport.send(json.dumps(payload, separators=(",", ":")))

    async def _consume(self, transport: WebSocketTransport, stop: asyncio.Event) -> None:
        heartbeat_started = datetime.now(UTC)
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(transport.recv(), self.heartbeat_timeout_seconds)
            except TimeoutError as exc:
                raise StaleMarketData("Coinbase heartbeat timeout") from exc
            payload = json.loads(raw)
            received_at = datetime.now(UTC)
            if self.recorder is not None:
                self.recorder.record("coinbase.websocket", payload, received_at=received_at)
            self._record_heartbeat(payload, received_at)
            heartbeat_reference = self.last_heartbeat or heartbeat_started
            if received_at - heartbeat_reference > timedelta(
                seconds=self.heartbeat_timeout_seconds
            ):
                raise StaleMarketData("Coinbase heartbeat is stale")
            await self._handle_payload(payload, received_at)

    def _record_heartbeat(self, payload: dict[str, Any], received_at: datetime) -> None:
        if payload.get("channel") == "heartbeats" or payload.get("type") == "heartbeat":
            self.last_heartbeat = received_at

    async def _handle_payload(self, payload: dict[str, Any], received_at: datetime) -> None:
        channel = payload.get("channel")
        for event in payload.get("events", []):
            if channel == "candles":
                for raw in event.get("candles", []):
                    product_id = raw.get("product_id")
                    if not product_id:
                        continue
                    candle = normalize_coinbase_candle(
                        product_id,
                        raw,
                        interval="ONE_MINUTE",
                        received_at=received_at,
                    )
                    self.last_candle_at[product_id] = candle.opened_at
                    if self.on_candle is not None:
                        await self.on_candle(candle)
            elif channel == "ticker":
                for raw in event.get("tickers", []):
                    product_id = raw.get("product_id")
                    if not product_id:
                        continue
                    quote = Quote(
                        symbol=product_id,
                        bid=Decimal(str(raw["best_bid"])),
                        ask=Decimal(str(raw["best_ask"])),
                        as_of=datetime.fromisoformat(raw["time"].replace("Z", "+00:00")),
                        source="coinbase-advanced-trade",
                        received_at=received_at,
                    )
                    self.last_quote[product_id] = quote
                    if self.on_quote is not None:
                        await self.on_quote(quote)

    def require_fresh_quote(self, product_id: str, *, now: datetime | None = None) -> Quote:
        quote = self.last_quote.get(product_id)
        current = now or datetime.now(UTC)
        if (
            quote is None
            or self.quote_age_seconds(product_id, now=current) > self.heartbeat_timeout_seconds
        ):
            raise StaleMarketData(f"no fresh quote for {product_id}")
        return quote

    def quote_age_seconds(self, product_id: str, *, now: datetime | None = None) -> float:
        quote = self.last_quote.get(product_id)
        if quote is None:
            return float("inf")
        current = now or datetime.now(UTC)
        return max(0.0, (current - quote.received_at).total_seconds())
