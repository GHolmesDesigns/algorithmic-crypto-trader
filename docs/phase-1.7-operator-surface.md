# Phase 1.7 operator surface

The operator surface is an authenticated, server-rendered view of trading state
and emergency controls. It is deliberately provider-neutral and accepts an
optional `BrokerInterface` and alert sinks from the application boundary.

## Authentication and authorization

- `OPERATOR_TOKEN` is required for operator access.
- `OPERATOR_ADMIN_TOKEN` is optional. When configured, re-arm requires the admin
  token while pause and emergency stop remain available to the operator token.
- `GET /operator/login` and its form POST establish an HttpOnly, SameSite
  session cookie. The dashboard never places a token in an HTML form action or
  rendered page. Header authentication (`x-operator-token`) remains available
  for API clients; a header request receives no cookie.
- `POST /operator/logout` revokes the session on the server and clears its
  cookie, so even a copy of the cookie is refused afterwards. Revocations are
  held in memory until the session would have expired; a restart forgets them,
  by which point the signed-out browser has already dropped its cookie.
- A `token` query parameter is refused with `400` on every authenticated route
  (decision for issue #60). Query tokens end up in browser history, proxy logs,
  and referrers, and no browser flow or script needs them: the browser uses the
  form and cookie, and scripts, including `deploy/drill.sh`, use the header.
- `GET /health` is liveness-only and does not expose trading state. Detailed
  health and strategy heartbeats require operator authentication.

## State and degraded behavior

`GET /operator` renders the dashboard, and `GET /operator/fragment` returns its
body without the page shell. The page does not refresh itself. `GET /operator/state` returns the same state as JSON.
The view includes trading mode, strategy version, application and broker
connectivity, strategy heartbeats, kill-switch state, balances, positions, P/L
availability, orders, fills, signals, alerts, and errors.

When the broker cannot be read, the surface reports `unavailable`, preserves any
previous values only as explicitly-labelled last-known data, and records a
redacted error condition. It never presents stale broker data as a current
successful refresh and never includes provider payloads or credentials.

## Controls and alert delivery

- `POST /operator/pause` sets the persistent kill switch to `paused`; it never
  lowers `halted`.
- `POST /operator/emergency-stop` sets it to `halted`.
- Each control, and sign-out, ends on a server-rendered result page (or JSON for
  a client that does not accept HTML): the action, resulting state, whether it
  changed, the UTC time, the acting role, the next safe step, and a return link.
- `GET /operator/rearm` is the administrator re-arm review. It repeats the
  startup recovery, reconciliation, pending or unknown order, and strategy
  heartbeat warnings, lists recent kill-switch changes, and shows the seven
  checklist items from the user manual as required checkboxes with a required
  cause-and-approval reference.
- `POST /operator/rearm` requires the administrator role. The server refuses,
  with the kill switch unchanged, a re-arm missing any checklist item or the
  reference (`422`), from an operator (`403`), or whose transition cannot be
  saved (`503`). The reference is redacted before it is saved.

## Kill-switch history

Every transition is saved to `system_events` (`event_type`
`kill_switch_transition`) with its previous and new state, the acting role
(`operator`, `admin`, or `system`), whether it was automatic, the reason, the
confirmed checklist for a re-arm, and the UTC time. The service lifespan attaches
this journal before startup recovery, so a recovery halt is saved, and loads the
recent history, which `/operator/state` returns newest first under
`risk.transitions`.

A stop never waits on the database: it takes effect and persists to the state
file first, and a save that fails is retried with the next transition. A re-arm
is saved first, together with any unsaved earlier transitions, and is refused
when that fails or no journal is attached.
- The `AlertRouter` fans out an alert to the explicitly injected phone-push and
  email sinks and records per-destination delivery status. Automated tests use
  deterministic recording sinks; no provider writes occur by default.

## Operational history

Issue #61. Authenticated, read-only views of the persisted audit tables. Each
route is a GET that answers JSON, or HTML when the request accepts `text/html`.

| Route | Shows |
| --- | --- |
| `/operator/history/orders` | Orders with their fills; each order expands into its lineage. |
| `/operator/history/orders/{client_order_id}` | One order's lineage: signal, risk decision, order, fills, and named gaps. |
| `/operator/history/signals` | Signals with their risk decision and order, and why no order was placed. |
| `/operator/history/risk-decisions` | Approvals and refusals with the failed gate and reason. |
| `/operator/history/risk` | The latest refusal, refusals grouped by the 17 ordered gates (`risk.engine.RISK_GATES`), and kill-switch history. |
| `/operator/history/discrepancies` | Reconciliation differences: entity, key, which fields differ, and the safety action. |
| `/operator/history/events` | System events; a kill-switch transition shows its known fields. |

Filters cover symbol, order status, time window, strategy version, failed risk
gate, correlation ID, and client order ID, where each applies. A refusal the
trading cycle records before the gates (`risk_inputs`) and any gate this build
does not recognise are counted separately from the 17.

**Bounds.** A window is `1h`, `24h` (default), `7d`, or `31d`, or `since` to
`until`, and never longer than 31 days. A page is 1 to 100 rows (default 25),
newest first, continued by a `before` cursor that keeps the first page's
`until`. Nested fills are capped at 50 per order, with the full count and filled
quantity reported. A wider window, a larger page, or an unknown, repeated, or
malformed parameter is refused with `422` before anything is read.

**Absence.** `200` with `"total": 0` means nothing was recorded in the window.
`503` means the history is not configured in this process or could not be read,
and the page says it is not a zero. Inside a lineage, a missing signal or risk
decision reads "not recorded", and gaps reuse `execution.audit.OrderLineage`.

**Safety.** The read model holds a session factory and nothing that reaches a
broker; no route can submit, cancel, or retry an order. Discrepancy payloads stay
in the database: only the names of the fields that differ are returned. Events
return only a kill-switch transition's from, to, actor, automatic flag, reason,
and known checklist items. Free-text reasons pass through `redact_free_text`.
An `unknown` order is critical and carries "Look it up by client order ID. Never
resubmit it."

**Indexes.** Migration `0006_history_indexes` indexes each time column the lists
window on, the lineage joins (`orders.signal_id`, `orders.risk_approval_id`,
`fills.order_id`), correlation-ID lookups, and `system_events(event_type,
created_at)`. Its upgrade and downgrade are tested, and a test checks that every
history query searches an index. The restart rehearsal reads a lineage back
after the app and Docker restarts.

## Trends

Issue #62. `/operator/history/trends` counts persisted rows per time bucket over
one window, as JSON or, when the request accepts `text/html`, as a page of
server-drawn SVG charts. It is authenticated, GET-only, and linked from the
history navigation and the dashboard's System health block.

| Chart | Source | Counts |
| --- | --- | --- |
| Reconciliation runs | `portfolio_snapshots` where `source = 'broker'`, by `recorded_at` | Completed reconciliations, at startup and on schedule. Each saves the broker's state once. A run that could not read the broker saves nothing and is not counted; the dashboard counts those since the process started. |
| Discrepancies by type | `discrepancies`, by `created_at` | One series per entity type (`order`, `fill`, `position`, `balance`), plus any type this build does not recognise. |
| Risk refusals by gate | `risk_decisions` where `approved` is false, by `decided_at` | One series per ordered gate, in `risk.engine.RISK_GATES` order, then `risk_inputs`, then any unrecognised or missing gate. |
| Uptime and freshness | none | **Not started.** Heartbeats, market-data state, and broker check times live only in the running process. Persisted candles cannot stand in: backfilled and live candles share one source name, and a backfilled candle's receipt time says when a gap was filled. |
| Equity and day P/L | none | **Not started.** Reconciliation saves no equity, and P/L stays unavailable until cost basis is known (#30) and a P/L producer exists. Rows already in `equity_curve` are not charted. |

**Bounds.** `window` is `24h` (default, 24 hourly bars), `7d` (28 six-hour bars),
or `30d` (30 daily bars). Bars align to UTC hours or days, and the last bar is the
current one, still in progress. `trends_query` refuses any window longer than 30
days or 30 bars, whatever the table offers. Any other window, a repeated window,
or any other parameter gets `422` before anything is read.

**Aggregation.** The database counts: each chart reads one indexed time range
(`ix_portfolio_snapshots_source_recorded_at`, `ix_discrepancies_created_at`,
`ix_risk_decisions_decided_at`) and returns one row per bucket and series. The
bucket is a `CASE` over the edges, computed in a subquery, because PostgreSQL
with server-side parameters cannot match a `GROUP BY` expression to the select
list's. Bucket `i` covers `edges[i]` up to, not including, `edges[i + 1]`, so a
row on an edge is counted once, in the later bucket. No schema change was needed.

**Zero, unavailable, not started.** A window that holds no rows reads "0 …
recorded in this window" and draws no axis. A read that fails, or a process
without the history database, answers `503` and each chart says "Not available",
never zero. A chart without a producer says "Not started" and why, on both the
`200` and `503` pages, and never draws an axis.

**Presentation.** Every series is drawn in one neutral ink (`--chart-ink`); green
and red stay reserved for system health. Each chart's SVG has `role="img"` and a
summary, and a table with the same counts follows it. In the discrepancy and
refusal tables, each period links to its records in the history list, whose
`until` stops one microsecond before the next bucket. There is no JavaScript and
no external resource.

**Safety.** Read-only. `SqlAlchemyTrends` holds a session factory and nothing
that reaches a broker. Only the time column and the series key leave the
database; payloads, reasons, and amounts do not.

## Research and replay workspace

Issue #64. `/operator/research` is an authenticated, server-rendered workspace
for bounded historical research. It exposes only repository-approved,
deterministic candle fixtures and two versioned reference strategies. A dataset
record includes its symbol, interval, window, approval note, and content digest.
The catalog is not a provider download surface and does not accept uploads.

The run form configures backtest or replay mode, initial cash, quantity, maker
and taker fees, spread, slippage, fee asset, and walk-forward training, test,
step, and sealed-holdout bars. A backtest uses `strategy.backtest.Backtester`;
a replay uses `app.replay.ReplayRunner` with `SimulatedBroker` and the same
cost assumptions. Neither path receives an application broker, database
session, credential, or network client.

Jobs are queued in memory and run in worker threads. The server caps each
dataset at 5,000 candles, each request at 24 walk-forward windows, and the
workspace at two active jobs and 24 retained jobs. A job never runs on the
trading loop, and a process restart intentionally forgets these research
records. The report includes the backtest's mandatory metadata, equity,
drawdown, exposure, distribution, trade count, yearly and regime breakdowns,
strategy version and hash, plus replay fills/refusals and parity when selected.

Every page and export says **Past results are not a promise of profit**. The
export is a whitelist-based JSON record: it contains the selected report and
assumptions, but no candle payload dump, provider response, credential,
recipient, or infrastructure identifier. Comparison accepts up to five
completed run IDs and compares strategy version, dataset, run type, final
equity, return, drawdown, exposure, and trade count.

| Route | Capability |
| --- | --- |
| `/operator/research` | Approved dataset catalog, run form, recent jobs, and comparison links. |
| `POST /operator/research/runs` | Queue one bounded backtest or simulator replay; JSON returns `202` with the job record. |
| `/operator/research/runs/{run_id}` | Read one queued, running, failed, or completed run. |
| `/operator/research/compare?run=...` | Compare up to five completed versioned runs. |
| `/operator/research/runs/{run_id}/export` | Download one completed, redacted JSON report. |

All research routes are authenticated. Invalid datasets, oversized windows,
too many walk-forward windows, and a full active queue are refused before the
job is started. Research is read-only with respect to providers and trading
controls.
