"""Coinbase public WebSocket ingestion with heartbeat and reconnect handling."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from core.disconnect import DisconnectReason, redact_diagnostic
from core.models import Candle, Quote
from websockets.exceptions import ConnectionClosed, InvalidStatus

from data.coinbase import COINBASE_WS_URL, GRANULARITY_SECONDS, normalize_coinbase_candle
from data.replay import JsonlReplayRecorder

logger = logging.getLogger(__name__)

# The Advanced Trade candles channel sends five-minute buckets, updated every second.
WEBSOCKET_CANDLE_INTERVAL = "FIVE_MINUTE"
_MALFORMED = (ValueError, KeyError, TypeError, ArithmeticError)


class WebSocketTransport(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class StaleMarketData(RuntimeError):
    """Raised when no heartbeat or quote has arrived within the freshness budget."""


class HeartbeatTimeout(StaleMarketData):
    """Raised when the stream goes quiet for longer than the heartbeat budget."""


class MalformedPayload(ValueError):
    """Raised when a stream message cannot be parsed into a candle or quote."""


def describe_exception(exc: BaseException) -> str:
    """One short line naming the failure; the caller redacts and bounds it."""

    if isinstance(exc, ConnectionClosed):
        received = exc.rcvd
        if received is None:
            return "connection closed without a close frame"
        return f"close code {received.code}: {received.reason or 'no reason given'}"
    if isinstance(exc, InvalidStatus):
        return f"{type(exc).__name__}: HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"


def classify_disconnect(exc: BaseException, stage: str) -> DisconnectReason:
    """Map the exception that ended a connection to a fixed kind and a redacted note."""

    kind = "unknown"
    if stage == "connect":
        kind = "connect_failed"
    elif stage == "subscribe":
        kind = "subscribe_failed"
    elif isinstance(exc, HeartbeatTimeout):
        kind = "heartbeat_timeout"
    elif isinstance(exc, StaleMarketData):
        kind = "stale_data"
    elif isinstance(exc, MalformedPayload):
        kind = "parse_error"
    elif isinstance(exc, ConnectionClosed):
        kind = "closed_by_peer"
    return DisconnectReason(kind, describe_exception(exc))


class CoinbaseWebSocketIngestor:
    def __init__(
        self,
        product_ids: tuple[str, ...],
        *,
        connect: Callable[[str], Awaitable[WebSocketTransport]] | None = None,
        on_candle: Callable[[Candle], Awaitable[None]] | None = None,
        on_quote: Callable[[Quote], Awaitable[None]] | None = None,
        gap_fill: Callable[[str, datetime | None, datetime], Awaitable[None]] | None = None,
        on_disconnect: Callable[[datetime, DisconnectReason], Awaitable[None]] | None = None,
        recorder: JsonlReplayRecorder | None = None,
        heartbeat_timeout_seconds: float = 30.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
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
        self.on_disconnect = on_disconnect
        self.recorder = recorder
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.reconnect_base_seconds = reconnect_base_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.last_heartbeat: datetime | None = None
        # Opening time of the newest bucket known to be closed, per product.
        self.last_candle_at: dict[str, datetime] = {}
        self._open_buckets: dict[str, Candle] = {}
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
            stage = "connect"
            try:
                transport = await self.connect(COINBASE_WS_URL)
                stage = "subscribe"
                await self._subscribe(transport)
                stage = "consume"
                await self._consume(transport, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The bucket in progress at the disconnect never closed on the stream.
                self._open_buckets.clear()
                reason = self._report_disconnect(exc, stage, attempts, delay)
                if self.on_disconnect is not None and not stop.is_set():
                    await self.on_disconnect(self.clock(), reason)
                if self.gap_fill is not None and not stop.is_set():
                    # Backfill closed buckets only: stop at the start of the current one.
                    gap_end = _bucket_start(self.clock())
                    last_filled = gap_end - timedelta(
                        seconds=GRANULARITY_SECONDS[WEBSOCKET_CANDLE_INTERVAL]
                    )
                    for product_id in self.product_ids:
                        previous = self.last_candle_at.get(product_id)
                        await self.gap_fill(product_id, previous, gap_end)
                        # The backfill emitted every bucket before gap_end; the stream
                        # must not emit one of them again.
                        if previous is None or last_filled > previous:
                            self.last_candle_at[product_id] = last_filled
                if stop.is_set():
                    return
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max_seconds, delay * 2)
            finally:
                if transport is not None:
                    await transport.close()

    @staticmethod
    def _report_disconnect(
        exc: Exception, stage: str, attempt: int, backoff: float
    ) -> DisconnectReason:
        """Classify and log the failure; nothing here may stop the reconnect."""

        try:
            reason = classify_disconnect(exc, stage)
        except Exception:
            reason = DisconnectReason("unknown", type(exc).__name__)
        try:
            logger.warning(
                "market-data disconnect kind=%s error=%s attempt=%d backoff=%.1fs note=%s",
                reason.kind,
                redact_diagnostic(type(exc).__name__),
                attempt,
                backoff,
                reason.note,
            )
        except Exception:
            pass
        return reason

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
                raise HeartbeatTimeout("Coinbase heartbeat timeout") from exc
            payload = _parse_message(raw)
            received_at = datetime.now(UTC)
            if self.recorder is not None:
                self.recorder.record("coinbase.websocket", payload, received_at=received_at)
            self._record_heartbeat(payload, received_at)
            heartbeat_reference = self.last_heartbeat or heartbeat_started
            if received_at - heartbeat_reference > timedelta(
                seconds=self.heartbeat_timeout_seconds
            ):
                raise HeartbeatTimeout("Coinbase heartbeat is stale")
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
                    try:
                        candle = normalize_coinbase_candle(
                            product_id,
                            raw,
                            interval=WEBSOCKET_CANDLE_INTERVAL,
                            received_at=received_at,
                        )
                    except _MALFORMED as exc:
                        raise MalformedPayload("candle payload could not be parsed") from exc
                    await self._update_bucket(product_id, candle)
            elif channel == "ticker":
                for raw in event.get("tickers", []):
                    product_id = raw.get("product_id")
                    if not product_id:
                        continue
                    observed_at = raw.get("time", payload.get("timestamp"))
                    if not isinstance(observed_at, str):
                        raise StaleMarketData("Coinbase ticker omitted its timestamp")
                    try:
                        quote = Quote(
                            symbol=product_id,
                            bid=Decimal(str(raw["best_bid"])),
                            ask=Decimal(str(raw["best_ask"])),
                            as_of=datetime.fromisoformat(observed_at.replace("Z", "+00:00")),
                            source="coinbase-advanced-trade",
                            received_at=received_at,
                        )
                    except _MALFORMED as exc:
                        raise MalformedPayload("ticker payload could not be parsed") from exc
                    self.last_quote[product_id] = quote
                    if self.on_quote is not None:
                        await self.on_quote(quote)

    async def _update_bucket(self, product_id: str, candle: Candle) -> None:
        """Hold the in-progress bucket; emit it only once a newer bucket starts."""

        closed = self.last_candle_at.get(product_id)
        if closed is not None and candle.opened_at <= closed:
            return  # already emitted, or backfilled after a reconnect
        current = self._open_buckets.get(product_id)
        if current is not None and candle.opened_at < current.opened_at:
            return  # a late update for a bucket that has already closed
        if current is not None and candle.opened_at > current.opened_at:
            self.last_candle_at[product_id] = current.opened_at
            if self.on_candle is not None:
                await self.on_candle(current)
        self._open_buckets[product_id] = candle

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


def _parse_message(raw: str | bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise MalformedPayload("message is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedPayload("message is not a JSON object")
    return payload


def _bucket_start(moment: datetime) -> datetime:
    seconds = GRANULARITY_SECONDS[WEBSOCKET_CANDLE_INTERVAL]
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, tz=UTC)
