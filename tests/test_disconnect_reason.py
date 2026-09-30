"""Every market-data disconnect records a fixed reason kind and a short redacted note."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from uuid import uuid4

import pytest
from core.disconnect import DISCONNECT_REASON_KINDS, NOTE_LIMIT, DisconnectReason, redact_diagnostic
from core.models import utc_now
from data import stream
from data.stream import CoinbaseWebSocketIngestor, classify_disconnect
from db.models import SystemEventRecord
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from tests.operator_support import history_app
from tests.test_history import BROWSER, add, get, tokens  # noqa: F401  (autouse fixture)

HOST = "ws-feed.exchange.coinbase.com"
ADDRESS = "203.0.113.9"
SECRET = "sk-live-9f2c7d1e"
HEARTBEAT = json.dumps({"channel": "heartbeats", "events": []})


class ScriptedTransport:
    """Replays messages, then applies ``ending``: raise it, or wait forever when None."""

    def __init__(self, messages=(), *, ending=None, send_error=None) -> None:
        self.messages = iter(messages)
        self.ending = ending
        self.send_error = send_error
        self.closed = False

    async def send(self, message: str) -> None:
        if self.send_error is not None:
            raise self.send_error

    async def recv(self) -> str:
        message = next(self.messages, None)
        if message is not None:
            return message
        if self.ending is not None:
            raise self.ending
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


async def first_disconnect(connect, *, heartbeat_timeout: float = 30.0) -> DisconnectReason:
    reasons: list[DisconnectReason] = []
    stop = asyncio.Event()

    async def on_disconnect(_at, reason: DisconnectReason) -> None:
        reasons.append(reason)
        stop.set()

    ingestor = CoinbaseWebSocketIngestor(
        ("BTC-USD",),
        connect=connect,
        on_disconnect=on_disconnect,
        heartbeat_timeout_seconds=heartbeat_timeout,
        reconnect_base_seconds=0.001,
    )
    await ingestor.run(stop, max_connections=1)
    assert len(reasons) == 1
    return reasons[0]


def serving(transport: ScriptedTransport):
    async def connect(_: str) -> ScriptedTransport:
        return transport

    return connect


def refusing(error: Exception):
    async def connect(_: str) -> ScriptedTransport:
        raise error

    return connect


@pytest.mark.asyncio
async def test_a_connect_failure_is_connect_failed_without_the_host_or_secret() -> None:
    error = OSError(f"cannot reach {HOST} at {ADDRESS} token={SECRET} via wss://{HOST}/ws")
    reason = await first_disconnect(refusing(error))

    assert reason.kind == "connect_failed"
    assert reason.note.startswith("OSError:")
    for leaked in (HOST, ADDRESS, SECRET):
        assert leaked not in reason.note


@pytest.mark.asyncio
async def test_a_rejected_handshake_names_the_http_status() -> None:
    response = Response(403, "Forbidden", Headers(), b"")
    reason = await first_disconnect(refusing(InvalidStatus(response)))

    assert (reason.kind, reason.note) == ("connect_failed", "InvalidStatus: HTTP 403")


@pytest.mark.asyncio
async def test_a_failed_subscribe_is_subscribe_failed() -> None:
    transport = ScriptedTransport(send_error=RuntimeError("subscribe refused"))
    reason = await first_disconnect(serving(transport))

    assert reason.kind == "subscribe_failed"
    assert transport.closed


@pytest.mark.asyncio
async def test_a_silent_stream_is_a_heartbeat_timeout() -> None:
    reason = await first_disconnect(serving(ScriptedTransport()), heartbeat_timeout=0.01)

    assert reason.kind == "heartbeat_timeout"


@pytest.mark.asyncio
async def test_a_ticker_without_a_timestamp_is_stale_data() -> None:
    ticker = json.dumps({"channel": "ticker", "events": [{"tickers": [{"product_id": "BTC-USD"}]}]})
    reason = await first_disconnect(serving(ScriptedTransport([ticker])))

    assert reason.kind == "stale_data"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "{not json",
        "[1, 2]",
        json.dumps({"channel": "candles", "events": [{"candles": [{"product_id": "BTC-USD"}]}]}),
        json.dumps(
            {
                "channel": "ticker",
                "timestamp": "2026-01-01T00:00:01Z",
                "events": [{"tickers": [{"product_id": "BTC-USD", "best_bid": "1"}]}],
            }
        ),
        json.dumps(
            {
                "channel": "ticker",
                "timestamp": "2026-01-01T00:00:01Z",
                "events": [
                    {"tickers": [{"product_id": "BTC-USD", "best_bid": "x", "best_ask": "2"}]}
                ],
            }
        ),
    ],
)
async def test_a_malformed_payload_is_parse_error(message: str) -> None:
    reason = await first_disconnect(serving(ScriptedTransport([HEARTBEAT, message])))

    assert reason.kind == "parse_error"


@pytest.mark.asyncio
async def test_a_peer_close_is_closed_by_peer_with_its_close_code() -> None:
    closed = ConnectionClosed(Close(1011, "server restarting"), None)
    reason = await first_disconnect(serving(ScriptedTransport([HEARTBEAT], ending=closed)))

    assert reason.kind == "closed_by_peer"
    assert reason.note == "close code 1011: server restarting"


@pytest.mark.asyncio
async def test_a_peer_close_without_a_frame_or_reason_is_still_closed_by_peer() -> None:
    bare = await first_disconnect(serving(ScriptedTransport(ending=ConnectionClosed(None, None))))
    silent = await first_disconnect(
        serving(ScriptedTransport(ending=ConnectionClosed(Close(1006, ""), None)))
    )

    assert (bare.kind, bare.note) == (
        "closed_by_peer",
        "connection closed without a close frame",
    )
    assert silent.note == "close code 1006: no reason given"


@pytest.mark.asyncio
async def test_an_unexpected_exception_is_unknown_and_the_event_is_kept() -> None:
    reason = await first_disconnect(serving(ScriptedTransport(ending=ZeroDivisionError("boom"))))

    assert (reason.kind, reason.note) == ("unknown", "ZeroDivisionError: boom")


@pytest.mark.asyncio
async def test_a_long_reason_is_truncated_never_rejected() -> None:
    reason = await first_disconnect(refusing(RuntimeError("long failure " * 500)))

    assert reason.kind == "connect_failed"
    assert len(reason.note) == NOTE_LIMIT
    assert reason.note.endswith("…")


def test_an_unrecognized_kind_becomes_unknown() -> None:
    assert DisconnectReason("made_up", "x").kind == "unknown"
    assert set(DISCONNECT_REASON_KINDS) >= {
        "connect_failed",
        "subscribe_failed",
        "heartbeat_timeout",
        "stale_data",
        "parse_error",
        "closed_by_peer",
        "unknown",
    }


def test_redaction_removes_addresses_hostnames_and_credentials() -> None:
    text = f"{HOST} {ADDRESS} 2001:db8::1 password={SECRET} ops@example.test wss://{HOST}/x"
    redacted = redact_diagnostic(text)

    for leaked in (HOST, ADDRESS, "2001:db8", SECRET, "ops@example.test"):
        assert leaked not in redacted
    assert redact_diagnostic("plain text") == "plain text"


def test_classify_uses_the_stage_before_the_exception_type() -> None:
    closed = ConnectionClosed(None, None)

    assert classify_disconnect(closed, "connect").kind == "connect_failed"
    assert classify_disconnect(closed, "subscribe").kind == "subscribe_failed"
    assert classify_disconnect(closed, "consume").kind == "closed_by_peer"


@pytest.mark.asyncio
async def test_each_disconnect_is_logged_at_warning_with_attempt_and_backoff(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="data.stream")
    error = OSError(f"cannot reach {HOST} token={SECRET}")
    stop = asyncio.Event()
    ingestor = CoinbaseWebSocketIngestor(
        ("BTC-USD",), connect=refusing(error), reconnect_base_seconds=0.001
    )

    await ingestor.run(stop, max_connections=2)

    records = [record for record in caplog.records if record.name == "data.stream"]
    assert [record.levelno for record in records] == [logging.WARNING, logging.WARNING]
    first, second = (record.getMessage() for record in records)
    assert "kind=connect_failed" in first and "attempt=1" in first and "backoff=0.0s" in first
    assert "attempt=2" in second
    assert "error=OSError" in first
    for record in records:
        for leaked in (HOST, SECRET):
            assert leaked not in record.getMessage()


@pytest.mark.asyncio
async def test_a_logging_failure_never_stops_the_disconnect_handler(monkeypatch) -> None:
    def broken(*_args, **_kwargs) -> None:
        raise RuntimeError("log sink is down")

    monkeypatch.setattr(stream.logger, "warning", broken)
    reason = await first_disconnect(refusing(OSError("down")))

    assert reason.kind == "connect_failed"


@pytest.mark.asyncio
async def test_a_classifier_failure_falls_back_to_unknown(monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(stream, "classify_disconnect", broken)
    reason = await first_disconnect(refusing(OSError("down")))

    assert (reason.kind, reason.note) == ("unknown", "OSError")


def disconnect_event(payload: dict, at) -> SystemEventRecord:
    return SystemEventRecord(
        event_id=uuid4(),
        event_type="disconnect",
        correlation_id=uuid4(),
        payload=payload,
        created_at=at,
    )


@pytest.mark.asyncio
async def test_system_events_shows_the_reason_and_filters_to_disconnects(tmp_path) -> None:
    application = history_app(tmp_path)
    now = utc_now()
    add(
        tmp_path,
        disconnect_event(
            {
                "at": now.isoformat(),
                "reason_kind": "connect_failed",
                "reason_note": f"InvalidStatus: HTTP 403 from {HOST} {ADDRESS} token={SECRET}",
            },
            now,
        ),
        disconnect_event({"at": now.isoformat()}, now - timedelta(minutes=1)),
        disconnect_event(
            {"at": now.isoformat(), "reason_kind": "made_up", "reason_note": "x"},
            now - timedelta(minutes=2),
        ),
        SystemEventRecord(
            event_id=uuid4(),
            event_type="gap_fill",
            correlation_id=uuid4(),
            payload={"symbol": "BTC-USD"},
            created_at=now,
        ),
    )

    everything = (await get(application, "/operator/history/events")).json()["rows"]
    only = (
        await get(application, "/operator/history/events", params={"event_type": "disconnect"})
    ).json()["rows"]
    gap = (
        await get(application, "/operator/history/events", params={"event_type": "gap_fill"})
    ).json()["rows"]
    page = (await get(application, "/operator/history/events", headers=BROWSER)).text

    assert len(everything) == 4
    assert [row["event_type"] for row in only] == ["disconnect"] * 3
    assert [row["event_type"] for row in gap] == ["gap_fill"]
    assert gap[0]["disconnect"] is None
    recorded, legacy, invalid = (row["disconnect"] for row in only)
    assert recorded["kind"] == "connect_failed"
    assert "HTTP 403" in recorded["note"]
    for leaked in (HOST, ADDRESS, SECRET):
        assert leaked not in json.dumps(only) and leaked not in page
    assert legacy == {"kind": "not_recorded", "note": ""}
    assert invalid["kind"] == "not_recorded"
    assert "Could not connect" in page and "Reason not recorded" in page
    assert "HTTP 403" in page
    for event_type in ("disconnect", "gap_fill", "heartbeat", "restart", "kill_switch_transition"):
        assert f'value="{event_type}"' in page
