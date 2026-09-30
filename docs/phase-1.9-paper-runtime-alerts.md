# Paper runtime and operator alerts

**Issue:** [#51](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/51)

**Scope:** prerequisite engineering for the #12 soak; no live-trading authorization

## Runtime path

With `TRADING_MODE=paper`, a configured paper broker, and
`PAPER_RUNTIME_ENABLED=1`, application startup now proceeds in this order:

1. recover persisted orders and broker-authoritative portfolio state;
2. start scheduled reconciliation and create its persistence-first
   `ExecutionEngine` and lock;
3. backfill a bounded five-minute Coinbase history window;
4. start the public Coinbase WebSocket for ticker and candle updates; and
5. for every newly closed live candle, validate the contiguous history, require a
   fresh public quote, and pass `MarketState` to the existing
   `MovingAverageCrossStrategy` and `TradingCycle`.

The cycle reuses the reconciler's execution engine and lock. No order can land
mid-reconciliation. Signals and risk decisions use the SQL audit store; orders
are persisted before provider submission. A disconnect drops its in-progress
bucket, gap-fills only closed buckets through bounded REST pages, and resumes
without replaying a stale signal.

Backtest, replay, and live modes do not start this runtime. This card does not
change the live-mode guards or authorize Coinbase order placement.

## Fail-closed behavior

- No configured broker: runtime remains disabled and the strategy heartbeat is
  unhealthy.
- Missing or stale public quote, incomplete/non-contiguous candles, or candle
  persistence failure: that candle does not reach `TradingCycle`; the runtime is
  degraded and alerts once per active condition.
- Invalid runtime or alert configuration: startup fails before HTTP service.
- Unexpected market-data task exit: the persistent kill switch trips to
  `HALTED`, the heartbeat becomes unhealthy, and a critical alert is routed.
- An unexpected exception escaping `TradingCycle` also trips `HALTED`; the
  alert requires broker-state review because submission may already have begun.
- A trading-cycle halt routes a critical alert. Reconciliation divergence and
  unavailability continue to route through the same `OperatorState`.
- A market-data reconnect storm is a warning when three or more disconnects
  occur within five minutes. The alert repeats no more often than every 15
  minutes while the storm persists, and clears only after the stream has
  received heartbeats continuously for 60 seconds. The reconnect delay also
  returns to its one-second base after that healthy period; repeated failures
  still back off to the 30-second ceiling.
- Alert delivery failure is recorded as `failed` for that destination. Tokens,
  passwords, topic URLs, and recipient addresses are not placed in alert text or
  logs.

## Alert destinations

Phone push uses an explicitly configured HTTPS ntfy topic. Email uses SMTP with
STARTTLS by default. Both may be enabled; one failing destination does not stop
the other.

Reconnect-storm defaults are bounded and can be overridden in the protected
deployment environment:

```text
PAPER_RECONNECT_STORM_THRESHOLD=3
PAPER_RECONNECT_STORM_WINDOW_SECONDS=300
PAPER_RECONNECT_STORM_ALERT_INTERVAL_SECONDS=900
PAPER_RECONNECT_HEALTHY_SECONDS=60
```

The Soak & readiness daily digest marks a UTC day with a rolling-window storm
as **Reconnect storm review** and keeps that day incomplete. The raw disconnect
count remains visible, but it is never treated as a passing soak day. Review
the System events rows for the bounded reason kind and note, confirm that REST
gap fill completed, and keep the paper runtime in its existing fail-closed
state. Do not re-arm a halt or change trading configuration merely to silence
the alert; escalate a persistent storm through the incident runbook.

Minimum ntfy configuration:

```text
ALERT_NTFY_TOPIC_URL=https://your-ntfy-host.example/private-topic
ALERT_NTFY_TOKEN=REPLACE_WITH_TOKEN_IF_REQUIRED
```

Minimum email configuration:

```text
ALERT_SMTP_HOST=smtp.example.com
ALERT_SMTP_PORT=587
ALERT_SMTP_STARTTLS=1
ALERT_EMAIL_FROM=trader@example.com
ALERT_EMAIL_TO=operator@example.com
```

If the server requires authentication, set both `ALERT_SMTP_USERNAME` and
`ALERT_SMTP_PASSWORD`. Keep every real value in the protected deployment
environment, never in Git or issue comments.

## Operator evidence

`/operator/state` and the server-rendered dashboard expose:

- runtime status and detail;
- last cycle status and timestamp;
- reviewed strategy version and heartbeat; and
- each alert's per-destination delivery result.

`/health` remains liveness-only. It does not prove that the feed, strategy,
broker, or alerts are healthy.

## Verification boundary

Automated tests use scripted candles, local stores, `httpx.MockTransport`, and a
fake SMTP server. They do not contact an exchange or notification provider. A
deployment of the final reviewed head, an end-to-end notification to the
owner's chosen destination, a real WebSocket disconnect, and elapsed 72-hour /
30-day evidence remain operator or soak verification, not local-test claims.
