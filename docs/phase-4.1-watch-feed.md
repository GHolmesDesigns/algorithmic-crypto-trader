# Phase 4.1: watch-only market-data feed and saved watchlist

Issue #87. Roadmap Rev. C, section 9. Public Coinbase market data only: this
change adds no credentials and no provider writes, and it changes nothing in
the trading symbols, strategy, risk, execution, or reconciliation.

## What it adds

- A `watchlist` table (migration `0009_watchlist`): at most nine unique
  `*-USD` symbols, each with its display position, when it was added, and the
  acting role. The table itself enforces the cap (`position` is unique and
  limited to 0-8).
- `/operator/watchlist`, a page and JSON route, and add, remove, and reorder
  POSTs. Each change writes a `watchlist_change` row to `system_events` in the
  same transaction as the change.
- On add, one public product lookup (`GET /market/products/{id}`) confirms the
  product exists and is `online`, not `trading_disabled`, not `is_disabled`, and
  a spot product. Anything else, including a lookup that could not answer, is
  refused with the reason and nothing is saved.
- A store-only feed, off unless `WATCH_FEED_ENABLED=1` or an administrator turns it on from the Watchlist page (issue #166, below). It collects closed
  five-minute candles for watched symbols that are not in `PAPER_SYMBOLS`, and
  writes them with the existing `SqlAlchemyCandleStore`. Nothing reads them yet.
- Per-symbol feed state in `GET /operator/state` under `watch_feed`, apart from
  `runtime` and the heartbeats: `fresh`, `stale`, `not_collected`, or
  `unavailable`, with the last candle time.

## Turning the feed on and off from the page (issue #166)

An administrator can start and stop the feed from the Watchlist page, with no
restart and no edit to the server's `.env`.

- `POST /operator/watchlist/feed` takes `enabled` (`1` or `0`, or JSON `true`
  or `false`). It needs the administrator token or session: another signed-in
  role gets 403 and nobody signed in gets 401. Asking for the state the feed is
  already in answers `unchanged` and writes nothing.
- The choice is one row in `operator_settings` (migration `0012`), saved in the
  same transaction as a `watch_feed_change` system event (action, previous and
  new value, role). If either write fails, nothing is saved and the feed is left
  as it was.
- At startup the saved choice decides. With none saved, `WATCH_FEED_ENABLED`
  decides, as before. A saved value that cannot be read leaves the feed off even
  when the environment says on, and the page and `GET /operator/state` say why
  (`watch_feed_setting`). It never defaults to on.
- `WatchFeedController` (`app/watch_feed.py`) owns the feed's task. Turning it
  off stops the task and keeps saved candles; pruning stops with it. Turning it
  on uses the same `WatchOnlyFeed`, its own client, limiter and breaker, and the
  30-day backfill described above.
- The controller and the setting store import nothing from strategy, risk,
  execution, brokers, or portfolio, and a test parses `data/feed_setting.py`
  like the other two modules. Toggling changes no trading symbol, risk decision,
  order, heartbeat, or kill-switch state.
- Not included: per-coin on/off, setting `WATCH_FEED_RETENTION_DAYS` from the
  page, and deleting saved candles when the feed is turned off.

## Isolation from trading

- `app/watch_feed.py` and `data/watchlist.py` import nothing from `strategy`,
  `risk`, `execution`, `brokers`, `portfolio`, or `app.trading`. A test parses
  their imports and fails if one appears.
- The feed takes no operator state, kill switch, or alert router. A failure
  sets that symbol's feed state and nothing else; tests show `runtime`, the
  `primary` heartbeat, the kill switch, alerts, and errors unchanged after 429,
  timeout, and unexpected failures.
- Another test replaces `TradingCycle.on_market_state`, both strategies'
  `on_market_state`, the risk `evaluate`, and `ExecutionEngine.submit` with
  functions that raise, runs the feed, and shows none of them is called.
- The trading symbols still come from `PAPER_SYMBOLS` alone
  (`parse_paper_symbols`). A symbol in both lists is collected once, by the
  trading feed; the watch-only feed skips it.
- The feed has its own `CoinbaseRESTClient`, `TokenBucketRateLimiter`, and
  `CircuitBreaker`. Its breaker opening never affects the trading feed's.

## Request budget

All requests are public and unauthenticated, through one limiter (burst 2,
refill 1 per second) and one breaker (opens after 5 failures, retries after 60
seconds), shared by the feed and the add-time lookup.

| Situation | Requests |
| --- | --- |
| Steady state, N watched symbols not traded | N every five minutes, at most 9 |
| Adding a symbol | 1 lookup, then a 30-day backfill of 29 requests (8,640 candles, 300 per request) |
| Worst case, nine new symbols at once | 261 requests, about four and a half minutes at the limiter's rate |
| A failing symbol | Retried after 1, 2, 4, 8, 16, then every 30 minutes; never sooner |

A cycle runs 15 seconds after each five-minute boundary, or immediately after
a coin is added. A young or quiet coin may return fewer candles than a full
window; each returned candle is validated on its own and the rest are simply
absent, so a gap is shown as a gap rather than failing the coin.

## Retention: proposal for the owner to approve

**Proposed: keep 30 days of five-minute candles for watched, non-traded
symbols.** That matches the backfill window, so a newly added coin and a coin
watched for a year both chart the same 30 days, at about 8,640 rows per coin
and about 78,000 rows for a full watchlist.

- `WATCH_FEED_RETENTION_DAYS` sets it (default 30, allowed 30-365).
- Pruning runs at most hourly, removes at most 5,000 rows per symbol per run,
  and reads the watchlist itself: it can only delete candles of symbols that
  are currently watched and not in `PAPER_SYMBOLS`. A trading symbol's candles
  are never pruned.
- **Decision for the owner:** candles of a coin removed from the watchlist stay
  in the table, because such a coin is no longer "watch-only" and the card
  limits pruning to watch-only symbols. If those rows should be cleared
  instead, that is a separate, explicit decision.

## Verification status

- Local validation, remote CI, and the restore and restart drills are recorded
  on the pull request.
- Owner-run verification: none is needed for the code. The first real
  Coinbase product lookup and candle collection happens only when the owner
  enables `WATCH_FEED_ENABLED` on a deployment; that has not been done, and no
  provider behavior is claimed here.
