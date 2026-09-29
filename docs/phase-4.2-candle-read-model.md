# Phase 4.2: bounded market-candle read model and JSON endpoint

Issue #88. Roadmap Rev. C, section 9. Read-only: this change adds no provider
access, no writes, and no change to strategy, risk, execution, or
reconciliation. It reads the `market_candles` table that the trading feed and the
watch-only feed (#87) already fill.

## The endpoint

`GET /operator/markets/candles`, operator role, JSON only. It is the one source
that the server-drawn tiles (#89) and the chart library (#90) will both read.

| Parameter | Values | Default |
| --- | --- | --- |
| `symbols` | One to nine `*-USD` symbols, comma-separated, each once | The saved watchlist; a `400` if it is empty or not configured |
| `window` | `24h`, `7d`, `30d`, `90d` | `24h` |
| `interval` | `15m`, `1h`, `6h`, `1d` | The finest interval that keeps a series within 300 points |

| Window | Default | Allowed intervals (points) |
| --- | --- | --- |
| `24h` | `15m` | `15m` (96), `1h` (24), `6h` (4), `1d` (1) |
| `7d` | `1h` | `1h` (168), `6h` (28), `1d` (7) |
| `30d` | `6h` | `6h` (120), `1d` (30) |
| `90d` | `1d` | `1d` (90) |

Bars are aligned to UTC (a daily bar is a UTC day; a six-hour bar starts at 00,
06, 12, or 18 UTC). The window is the last N bars, ending with the bar that
contains now, so its first edge is bar-aligned rather than exactly 24 hours ago.
The response states `since`, `until`, `as_of`, and `in_progress_from`.

Refusals are `400` with `{"detail": {"status": "refused", "errors": [...]}}`,
every reason listed together: an unknown parameter, a repeated one, an unknown
window or interval, a window and interval that exceed 300 points, more than nine
symbols, a duplicate symbol, or a symbol that is not a USD product. A
`?token=` in the URL is `400` as on every other route; a missing or wrong
token is `401`. Candles that cannot be read are `503`, never an empty chart. The
route has no write method.

## The payload

Per symbol, in the order requested:

- `bars`: one per bucket in the window, each with `opened_at`, `closed_at`,
  `state`, `candles`, `expected`, and `open`, `high`, `low`, `close`, `volume`
  as decimal strings. Open is the first candle's open, close the last candle's
  close, high the maximum, low the minimum, and volume the sum.
- Bar `state` is one of `complete`, `partial` (a finished bar with fewer than
  `expected` candles), `in_progress` (the bar now running), or `gap` (a finished
  bar with no candles). A gap and an in-progress bar with no candles carry
  `null` values, never zero and never the previous close.
- `gaps`: runs of consecutive gap bars, each `from`, `to`, and `bars`. The bar
  now running is never a gap.
- `freshness.state`: `fresh`, `stale` (the newest candle closed more than three
  intervals ago, the same limit the feed uses), `not_collected` (no candle has
  ever been stored), or `unavailable` (the watch-only feed reports this symbol
  failing, with its reason in `detail`). `freshness.last_candle_at` is the
  newest stored candle, wherever it falls.
- `in_window`: `candles`, `expected`, and `empty`. **"No rows in the window" is
  `in_window.empty`, which is separate from `freshness`:** a symbol can be
  `fresh` with an empty window (asked about an earlier period), or `stale` with a
  full one.
- `source`: the newest stored candle's source, or `null` if none.

## How it reads

Two grouped reads and one small lookup, all `SELECT`, all on the
`(symbol, interval, opened_at)` unique key and only `FIVE_MINUTE` rows:

1. One grouped query over all requested symbols returns, per symbol and bar, the
   candle count, first and last candle time, high, low, and volume sum. The
   database assigns the bar, so no candle row leaves it (a 90-day, nine-symbol
   read touches about 233,000 rows and returns at most 810 groups).
2. One query returns each symbol's newest candle time.
3. One lookup fetches the open of each bar's first candle, the close of its last
   one, and the newest candle's source, by symbol and time: at most two rows per
   bar.

Values are `Decimal` end to end and leave as plain decimal strings without an
exponent or rounding, so a coin priced below $0.0001 keeps every digit. No
floats are used in the module.

## Limits worth knowing

- **Retention decides how far back a watch-only coin goes.** Watch-only candles
  are kept for `WATCH_FEED_RETENTION_DAYS` (default 30), so a 90-day read of such
  a coin shows about 60 days of gaps until retention is raised. That is shown as
  gaps, not hidden.
- **Test dialect.** Tests run on SQLite, which passes numeric values through
  floating point, so fixtures use values that are exact in binary or that survive
  the round trip. PostgreSQL, used in deployment, keeps `NUMERIC` exact.
- The candle endpoint is not paged; the caps bound it instead.

## Verification status

- Local validation, remote CI, and any drill are recorded on the pull request.
- Owner-run verification: none. The endpoint reads only the app's own database
  and touches no provider. No provider behavior is claimed here.
