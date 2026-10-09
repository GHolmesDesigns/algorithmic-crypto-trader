# Algorithmic Crypto Trader user manual

- **Manual version:** Draft 1
- **Applies to:** application version `0.1.0` at repository commit `52d016a`
- **Last reviewed:** 2026-09-29

<!-- markdown-only-note:start -->
Open the [styled HTML manual](user-manual.html) for audience filters, responsive
navigation, dark-mode support, and print formatting. This Markdown file remains
the canonical editable source.
<!-- markdown-only-note:end -->

> **Safety notice**
>
> This project is experimental software, not financial advice. The current
> repository does not authorize live trading. Never paste exchange credentials,
> operator tokens, account details, database exports, or unredacted operational
> logs into this repository, an issue, a pull request, a chat, or an AI prompt.

## Choose your path

- If you operate the service and do not need to change code, start with
  [Part I: Operator guide](#part-i-operator-guide).
- If you are an AI agent, Python contributor, or .NET engineer integrating with
  the service, start with [Part II: Engineering and agent guide](#part-ii-engineering-and-agent-guide).
- If the service is in an unsafe or uncertain state, go directly to
  [Incident response](#incident-response).

## What the current application does

The service provides a safety-first foundation for research, simulated trading,
Gemini Sandbox rehearsal, and a separately guarded Coinbase live path. On a
normal service start it:

1. validates the selected mode and credential scope;
2. loads the persistent kill switch;
3. constructs only the broker allowed for that mode;
4. recovers pending or unknown orders by their persisted client order ID;
5. reconciles persisted state with the broker, when a broker is configured;
6. starts scheduled reconciliation when a broker is configured; and
7. exposes health checks, an authenticated operator dashboard, bounded,
   read-only history of the persisted signals, risk decisions, orders, fills,
   discrepancies, and events, and trends that count them over time.

The broker is authoritative during reconciliation. If the database and broker
disagree, or if required state cannot be read, the system fails closed and
halts trading.

### Current release boundary

This draft describes the software as it exists today, not the final planned
product:

- In `paper` mode, a configured broker plus `PAPER_RUNTIME_ENABLED=1` starts the
  public Coinbase feed and drives the existing `TradingCycle` from closed
  five-minute bars. Other modes do not start this runtime, and this feature does
  not authorize or enable live trading.
- The operator surface reports the paper runtime, its last cycle outcome, and the
  primary strategy heartbeat. P/L can still be unavailable.
- Phone-push through ntfy and email through SMTP are optional. The application
  configures only the destinations whose complete environment settings are
  present; partial or insecure configuration stops startup.
- Owner-run provider evidence is only partly complete. On 2026-09-27 the Gemini
  Sandbox check passed except for a partial fill, and Coinbase read-only
  reconciliation passed. Still required: the adapter contract suite against the
  real venues, the 72-hour market-data run, and the seven-day unattended
  reconciliation soak. See [owner-run exchange checks](owner-run-exchange-checks.md)
  and the [Phase 1 gate ledger](phase-1-gate-acceptance.md).
- Coinbase order placement remains a later, explicitly authorized phase. Do not
  interpret the presence of the adapter or live-mode guards as permission to use
  them.

# Part I: Operator guide

## Your responsibilities

As an operator, you monitor the system, pause or stop it when something is
uncertain, and re-arm it only after the cause is understood. You do not need to
read code or use a command line for routine dashboard operation.

You should receive the following through a private channel from the system
owner or administrator:

- the operator dashboard address;
- either an operator token or an administrator token;
- the expected trading mode; and
- the person or team to contact during an incident.

Treat the token like a password. Do not store it in browser bookmarks, URLs,
screenshots, tickets, or chat messages.

## Key terms

| Term | Plain-language meaning |
| --- | --- |
| Trading mode | The environment in which the system is allowed to operate. |
| Broker | The simulated or external venue that holds balances, orders, and positions. |
| Kill switch | The persistent safety control that determines whether new trading work may proceed. |
| Startup recovery | The check performed before the service accepts requests. It resolves uncertain orders and compares saved state, plus the app's own fills saved since the last snapshot, with the broker. |
| Reconciliation | A repeated comparison between local records and the broker's records. |
| Client order ID | The system's unique, persisted identifier for an order. It is used to find an uncertain order without submitting a duplicate. |
| Fail closed | Stop or refuse activity when required information is missing, stale, or uncertain. |

## Trading modes

| Mode | Purpose | External orders? |
| --- | --- | --- |
| `backtest` | Run a strategy over historical data. | No |
| `replay` | Feed recorded market data through the normal strategy, risk, and execution path. | No |
| `paper` | Rehearse using a simulator or Gemini Sandbox. | Never a production Coinbase order |
| `live` | Separately guarded Coinbase production path. | Potentially, but not authorized by this manual or the current project phase |

If the dashboard shows a mode other than the one you were told to expect,
select **PAUSE** and contact the administrator. If it unexpectedly shows
`live`, select **EMERGENCY STOP**.

## Kill-switch states

| State | Meaning | Operator action |
| --- | --- | --- |
| `running` | The safety control permits work, subject to every other risk gate. It does not prove that a strategy is active. | Continue monitoring. |
| `paused` | New trading work should not proceed. Use this for planned investigation or uncertainty that is not yet an emergency. | Investigate; do not re-arm until the cause is understood. |
| `halted` | The system or an operator identified a critical condition. Automatic processes cannot re-arm it, and an operator cannot lower it to `paused`. | Escalate and keep it halted until the re-arm checklist is complete. |

The kill-switch state is stored across application and host restarts when the
deployment uses the configured persistent volume. Every change is also saved in
the database with its previous and new state, who or what made it (an operator,
an administrator, or the system), whether it was automatic, the reason, and the
UTC time.

## Sign in

1. Open the private service address supplied by the administrator and add
   `/operator/login` if it is not already present.
2. Confirm that the browser shows the expected secure site. In production, do
   not continue through a certificate warning.
3. Enter the token you received through the approved private channel.
4. Select **Sign in**.

The browser receives a protected session cookie that lasts up to eight hours.
The dashboard does not refresh itself: select **Refresh** in its header for a
new snapshot.

The server refuses a token in the address (`?token=`) with `400`, because
addresses are recorded in history and logs. Sign in through the form only.

## Sign out

Select **Sign out** in the header of any operator page. The server ends that
session and clears its cookie, and the next request with it is refused, even
from a copy of the cookie. Other sessions stay signed in. Always sign out on a
shared device, then close the tab.

## Read the dashboard

The dashboard is one server-rendered page. It works without JavaScript and loads
no outside scripts, fonts, or stylesheets. It shows everything that the
authenticated `/operator/state` returns:

- **Header:** trading mode, credential scope, the role the server granted you
  (**Operator** or **Administrator**), the snapshot time, **Refresh**, and
  **Sign out**. When only `OPERATOR_TOKEN` is configured, that token has the
  administrator role.
- **Section links:** Overview, Portfolio, Activity, Alerts & errors, Risk &
  safety, and System health. A wide screen shows them in a left rail; a phone
  shows a row that scrolls sideways.
- **Safety bar:** the kill-switch state, its cause, the next safe step, and the
  **PAUSE** and **EMERGENCY STOP** controls. It stays at the top of the window
  while you scroll. On a phone, the two controls stay fixed at the bottom of the
  screen.

Times are shown in UTC with their age when the page loaded. Select **Refresh**
for newer values.

### Status marks

Every status pairs a colour with a word and a mark. Read the word: the colour
and mark only help you scan.

| Mark | Meaning |
| --- | --- |
| ● | OK. |
| ▲ | Warning. |
| ■ | Critical. |
| ○ | Unknown or unavailable. |
| ◐ | Neutral: information that is not a health verdict, such as an open order or current portfolio data. |

The mode badge uses ◆ and turns red for `live`.

The page keeps these cases apart:

- A number, including `0`, is a real recorded count or value.
- **○ unavailable** means the source could not be read. It is never a zero.
- **○ unknown**, **not recorded**, and **never** mean the system cannot say yet.
- **▲ last known** marks values kept from an earlier successful broker read.
  They sit in a dashed table and are for diagnosis only.
- **◐ current** portfolio data means the broker was read at the snapshot time.
  It is about freshness, not correctness.

### The cause of a pause or halt

The safety bar lists each cause it can find in the current state: a halted
startup recovery, a diverged or unavailable scheduled reconciliation, or a
failed or halted paper runtime. It also shows the **Last change**: the saved
transition into the current state, with who or what made it, whether it was
automatic, the time, and the reason. That covers an operator control and the
kill-switch file or environment flag too.

When no saved transition explains the state, for example one set before
transitions were saved, the bar says the cause is not recorded. Check
**Alerts & errors** and the application log.

### What to check

Review these items from top to bottom:

| Dashboard item | Healthy or expected | Stop and investigate when |
| --- | --- | --- |
| Application | `healthy` when the broker refresh succeeds; liveness is separately available at `/health`. | The page cannot load or the service repeatedly restarts. |
| Trading mode | Matches the owner-approved mode. | It differs from the approved mode or unexpectedly says `live`. |
| Strategy version | Matches the reviewed release (**System health**). | It changed unexpectedly or an enabled strategy reports an unknown version. |
| Broker | `healthy` with a recent check time, or `not_configured` for an intentionally credential-free deployment. | `unavailable`, especially if positions or orders could exist. |
| Kill switch | The expected state in the safety bar. | It changes without an understood operator action or incident. |
| Startup recovery | `reconciled`, or `no_broker` when intentionally running without a broker and with no pending orders. **System health** shows its counts and completion time. | `halted`, or the detail mentions a pending order, missing baseline, unavailable broker, or divergence. |
| Reconciliation | `clean`, with a recent last run and no differences. `not_scheduled` without a broker. | `diverged` or `unavailable`, or the last run is old. |
| Paper runtime | `running` with a recent last cycle, or `disabled` when it is intentionally off. | `degraded`, `failed`, `halted`, or `stopped`, or `not_started` when it should run. |
| Portfolio data | **◐ current** when a broker is configured. | **▲ last known** or **○ unavailable** when broker state is required. |
| P/L | May be **○ unavailable** in the current release. It is never shown as `0`. | Do not infer profitability or safety from a missing value. |
| Balances and positions | Plausible and consistent with the approved environment. An average price the venue did not report shows **○ not known**. | A value is unexpected, duplicated, missing, or clearly belongs to another environment. |
| Orders, fills, and signals | Counts and recent activity match expectations. | An order is `unknown` (look it up by client order ID and never resubmit it), pending unexpectedly, duplicated, or lacks an expected fill. |
| Strategy heartbeats | An enabled paper strategy reports a recent healthy heartbeat and cycle result. | A heartbeat is absent, stale, unhealthy, or unexpectedly changes version. |
| Alerts and errors | Empty, or a previously reviewed condition. Each alert shows whether each destination was sent or failed. | Any new critical alert, failed delivery, broker error, reconciliation divergence, or repeated error. |
| Alert destinations | Phone push and email show **configured** where the deployment expects them. The page never shows recipients, topic addresses, or tokens. | A destination you rely on shows **not configured**: alerts then reach nobody. |

`/health` proves only that the web process responds. It does not prove that the
broker, strategy, market data, database, reconciliation, or trading path is
healthy.

## Review the history

The dashboard shows what this process has held since it started. The history
pages read the persisted records, so they cover earlier runs and survive a
restart. Open them from the dashboard's **Activity** or **Risk & safety**
section, or go to `/operator/history/orders`.

| Page | What it answers |
| --- | --- |
| **Orders** | Every order, newest first, with its status and fills. Select an order to expand its lineage: signal, risk decision, order, and fills. **Open this order's lineage page** gives a page you can link to. |
| **Signals** | What became of each signal: an order, or the gate that refused it. |
| **Risk decisions** | Every approval and refusal, with the gate that failed and why. |
| **Risk & safety** | The latest refusal, refusals counted under each of the 17 ordered gates, and kill-switch history. |
| **Discrepancies** | What reconciliation found different, and the safety action taken. Values stay in the database; only the names of the fields that differ are shown. |
| **System events** | Kill-switch changes and other recorded events. |

Filter by symbol, status, strategy version, failed gate, client order ID or
correlation ID, and time window. Times are UTC. A window covers at most 31 days
and a page at most 100 rows; the server refuses anything wider and reads
nothing. Select **Older** to page back and **Newest** to return.

Client order, correlation, signal, and approval IDs select whole with one click,
so you can copy them without JavaScript.

Read these the same way as the dashboard:

- **0 … recorded in this window** is a real zero.
- **○ Not available** means the history could not be read. It is not a zero.
- **Signal not recorded**, **Risk decision not recorded**, or a **▲ Lineage
  gap** means a link in the audit chain is missing. Report it as an incident.
- An **■ unknown** order carries its rule: look it up by client order ID and
  never resubmit it.

The history is read-only. Nothing on these pages can submit, cancel, or retry an
order.

## Read the trends

**Trends** counts the persisted records over time. Open it from the history
navigation or the dashboard's **System health** section, or go to
`/operator/history/trends`. Choose **Last 24 hours** (hourly bars), **Last 7
days** (6-hour bars), or **Last 30 days** (daily bars). Times are UTC, and the
last bar is the current period, still in progress.

| Chart | What it counts |
| --- | --- |
| **Reconciliation runs** | Completed reconciliations, at startup and on schedule. A run that could not read the broker is not counted here; the dashboard counts those since the process started. |
| **Discrepancies by type** | Differences reconciliation recorded, one row each, split into order, fill, position, and balance. |
| **Risk refusals by gate** | Refusals under the first gate that stopped them, in the gates' order. Gates with none are listed under **0 refusals at the other … gates**. |
| **Uptime and freshness** | **Not started.** Nothing saves heartbeats or freshness checks yet, so there is no history to chart. |
| **Equity and day P/L** | **Not started.** No trustworthy producer records them; P/L waits on known cost basis (#30). |

Each chart names the table it counts. Bars are drawn in one neutral ink: a tall
bar is a count, not a health verdict. Open **Table** under a chart for the same
counts as text. In the discrepancy and refusal tables, select a period to list
its records on the history page.

Read the states the same way as the history:

- **0 … recorded in this window** is a real zero. No axis is drawn.
- **○ Not available** means the records could not be read. It is not a zero.
- **◐ Not started** means no producer records this yet. It is not a zero either.

A gap in **Reconciliation runs** is a period without a completed run. Check
**Alerts & errors** and the application log for that time.

## Choose coins to watch

The **Watchlist** page saves up to nine coins you want to chart without
trading them. Open it from the dashboard's navigation or go to
`/operator/watchlist`.

Enter a Coinbase product such as `ETH-USD` and select **Add to watchlist**.
Before saving, the service asks Coinbase's public product list, with no
credentials, whether the product exists and is trading. It refuses, and says
why, when the product is unknown, delisted, or trading-disabled; when the list
already holds nine coins; when the coin is already on it; and when Coinbase
cannot confirm the product. A coin that could not be confirmed is not saved.
Use **Up**, **Down**, and **Remove** to reorder or drop a coin. Every change is
recorded as a `watchlist_change` event, with the role that made it, under
**System events**.

Watching is not trading. A watched coin is never added to `PAPER_SYMBOLS`, and
the strategy, risk gates, and execution never see its candles. To trade a coin
you still change `PAPER_SYMBOLS`, which is a deployment decision.

Saving a coin does not collect anything by itself. The **watch-only feed**
collects each watched coin's closed five-minute candles when the service starts
with `WATCH_FEED_ENABLED=1`. It is off by default. With it on:

- Adding a coin backfills 30 days of candles, 29 requests of 300 candles each,
  then adds one request per coin every five minutes.
- A coin that is also in `PAPER_SYMBOLS` is collected once, by the trading feed.
- Requests use Coinbase's public market data only, at most one a second on
  average, through their own request limiter and circuit breaker, separate from
  the trading feed's. A failing coin is retried after 1, 2, 4, and up to 30
  minutes, never faster.
- Candles older than `WATCH_FEED_RETENTION_DAYS` (default 30) are removed, for
  watched coins that are not trading symbols only.

Each coin shows its own feed state. It is kept apart from the dashboard's
runtime status and heartbeats, so a problem here never changes them:

| State | Meaning |
| --- | --- |
| **fresh** | The newest closed candle is recent. |
| **stale** | The newest closed candle is more than 15 minutes old. A coin that rarely trades can show this while healthy. |
| **not yet collected** | Nothing has been collected yet, or the feed is off. |
| **unavailable** | The last request failed. The reason and the retry delay follow the failure. Trading is unaffected. |

## Read the Markets page

The **Markets** page draws the saved coins side by side from the app's stored
five-minute candles. Open it from the operator navigation or go to
`/operator/markets`. It is read-only: it never changes the watchlist, adds a
symbol to `PAPER_SYMBOLS`, or submits an order.

Choose a window and bar interval, then select **Refresh charts**. The server
keeps each series within 300 points. The `c` field in the URL is a shareable
layout, for example `/operator/markets?c=BTC-USD,ETH-USD`; it does not save
anything. A layout may contain at most nine unique `*-USD` symbols. Invalid,
duplicate, or oversized layouts are refused with a reason before candles are
read. Use **Watchlist editor** to change the saved set.

Each tile shows the last price, change over the selected window, high, low,
source, and the time of the newest stored candle. The server draws the price
line as SVG, and missing candles break the line rather than becoming zero or
being carried forward. The **Accessible data table** under each tile repeats
the values in text. The page works with JavaScript disabled and embeds no
 TradingView script, iframe, image, or data feed. A **TradingView** link opens
 the corresponding hosted chart in a new tab only.

When the locally served chart library and same-origin data request both
succeed, a tile may add crosshair, zoom, pan, line/candle, and volume controls;
the table remains the accessible alternative. A visible TradingView attribution
link is required by the library license. No TradingView account, feed, or
runtime request is used. If the library is blocked, the JSON request fails, or
the response is malformed, the server-drawn SVG stays in place.

The interactive enhancement is limited to this page. Its Content-Security-Policy
permits scripts and chart data only from the app's own origin, disallows inline
script, and prevents framing. Every other operator page and every safety
control remains JavaScript-free. The pinned library, checksum, license, and
notice are in `api/static/markets/`; update them only in a reviewed pull request
that records the new release and SHA-256.

The tile states are deliberately different:

| Tile state | Meaning |
| --- | --- |
| **Drawn** | Recent stored candles produced a server-drawn line. |
| **Stale** | Stored candles exist, but the newest one is outside the freshness limit. |
| **Not yet collected** | No candle has been stored for the coin, or the watch-only feed is off. |
| **Unavailable** | The watch-only feed reported a failure; the reason is shown and no line is drawn. |

States do not change the dashboard's runtime, heartbeat, kill switch, alerts,
or trading symbols. A tile with no candles in the selected window says so
instead of showing an empty axis.

## Research and replay

The **Research and replay** page is an authenticated workspace for engineers
and operators reviewing approved historical data. It is read-only: a run never
contacts a broker, places an order, changes the trading mode, or pauses the
system. Open it from the dashboard or go to `/operator/research`.

Choose an approved dataset and a versioned reference strategy, then choose a
**Backtest** or a **Replay through the simulator**. You can set initial cash,
quantity, maker and taker fees, spread, slippage, fee asset, and walk-forward
training, test, step, and sealed-holdout bars. The page identifies the dataset
window, interval, approval note, and content digest before you run it.

Runs are bounded background jobs, so a long research run does not block the
dashboard, trading loop, or safety controls. The server allows at most 5,000
candles, 24 walk-forward windows, and two active jobs. Refresh the run page to
see whether it is queued, running, complete, or failed.

The completed report shows equity, return, drawdown and duration, exposure,
trade count, return distribution, yearly and regime breakdowns, strategy
version and hash, and the exact cost assumptions. Replay adds signals, fills,
risk refusals, and the difference from the backtest result. Use **Compare
selected runs** to compare up to five completed runs by strategy version,
dataset, run type, final equity, return, drawdown, exposure, and trade count.

Use **Export redacted report** for a JSON record that is safe to retain in the
private research log. It contains no provider response, credential, recipient,
or infrastructure identifier. Every page and export carries the warning:
**Past results are not a promise of profit.** A historical result is a record
of the selected window and assumptions, not a profitability promise.

## Routine operating check

At the beginning of a monitoring period:

1. Sign in and confirm the expected trading mode.
2. Confirm the kill-switch state.
3. Read the full startup-recovery status and detail.
4. Confirm broker connectivity and its check time.
5. Confirm balances and positions are plausible for this environment.
6. Review orders, fills, signals, strategy heartbeats, alerts, and errors.
   On the history pages, check **Risk & safety** for new refusals and
   **Discrepancies** for new differences.
7. When a broker is configured, confirm that scheduled reconciliation is
   `clean` and that its last run is recent. On **Trends**, confirm that
   **Reconciliation runs** has no unexplained gap.
8. Record only the approved, redacted result in the private operations log.

Do not mark the system healthy solely because `/health` returns a success.

## Controls

Every control ends on a result page that works without JavaScript. It shows the
action, the resulting kill-switch state, whether anything changed, the UTC time,
the role that acted, the next safe step, and a link back to the dashboard.

### Pause

Use **PAUSE** when you need time to investigate, when expected data is missing,
or before a planned maintenance action. Both operator and administrator tokens
can pause.

**PAUSE** never lowers a stricter state. While the system is halted, the
dashboard shows "administrator re-arm required" in place of **PAUSE**. A pause
request sent anyway leaves the system halted and reports "Already halted".
Pausing a system that is already paused changes nothing.

### Emergency stop

Use **EMERGENCY STOP** immediately when:

- the mode or credentials appear wrong;
- an order may be duplicated or its result is uncertain;
- balances or positions do not match the broker;
- the database, broker, market data, or risk inputs cannot be trusted;
- the service behaves unexpectedly after a restart; or
- you are unsure whether continued operation is safe.

Both operator and administrator tokens can stop the system. A halted state
cannot be cleared automatically or by an operator. Selecting **EMERGENCY STOP**
again while halted changes nothing.

### Re-arm

Only an administrator can re-arm when a separate administrator token is
configured. Re-arming changes the kill switch to `running`; it does not repair
a broker, database, data feed, strategy, or unresolved order. It is the only
control that lowers the kill switch.

While the system is paused or halted, **Risk & safety** shows the checklist and
a **Review and re-arm** link for an administrator. The review page repeats the
active warnings before you decide:

- startup recovery status and detail;
- the last scheduled reconciliation result;
- the saved orders that are still pending or unknown; and
- the strategy heartbeat.

It also shows the kill switch's recent saved changes.

Complete every item before re-arming. Each is a required checkbox on the review
page, and the server refuses a re-arm with any item unconfirmed:

1. Identify and document the original cause.
2. Confirm the approved mode and credential scope.
3. Resolve every pending or unknown order by looking it up at the broker with
   its persisted client order ID. Never submit it again merely because the first
   response was uncertain. An order the broker never received is closed from the
   dashboard (see [An order the venue never received](#an-order-the-venue-never-received)).
4. Confirm the broker-authoritative balances, positions, orders, and fills.
5. Confirm startup recovery and reconciliation are clean.
6. Confirm required market data and risk inputs are current.
7. Obtain the incident owner's approval when the operating procedure requires it.

Then enter the **cause and approval reference**, for example an incident or
ticket reference and who approved. It is required and limited to 500
characters. Do not include tokens, account identifiers, addresses, or provider
payloads: the server removes secrets, URLs, email addresses, and long
identifiers before it saves the reference with the transition.

Select **RE-ARM**. The result page reports `running`; then confirm on the
dashboard that no new error or divergence appears.

The server refuses a re-arm, and leaves the kill switch unchanged, when:

- any checklist item or the reference is missing (`422`; the review page shows
  what is missing and keeps what you entered);
- the caller is not an administrator (`403`); or
- the transition cannot be saved to the database (`503`). A re-arm that cannot
  be audited does not happen.

If any item is unknown, leave the system paused or halted.

## Incident response

### Broker is unavailable

1. Select **PAUSE**. Use **EMERGENCY STOP** if an order could be in flight.
2. Treat displayed balances and positions as last-known diagnostic data, not
   current truth.
3. Do not retry an order.
4. Ask an engineer to verify network access, provider status, and the exact
   client order IDs of any uncertain orders.

### Startup recovery is halted

1. Leave the system halted.
2. Record the recovery status and redacted detail.
3. Ask an engineer to inspect the persisted orders, portfolio baseline, and
   broker state.
4. Re-arm only after the broker and local state reconcile cleanly.

A restart shortly after the app's own trade no longer causes this by itself:
recovery counts the app's own fills saved after the last snapshot. If recovery
still halts with a divergence, the difference is something those fills do not
explain.

### Reconciliation reports a divergence

1. Leave the system halted; the broker's record is authoritative.
2. Record the number and type of differences without copying credentials,
   account identifiers, or raw provider payloads.
3. Have an engineer review the discrepancy event and the adopted broker state.
4. Do not edit the database to make the alert disappear.

A divergence is expected when something the app did not place touches the
account it watches: a manual trade, or the owner-run Gemini Sandbox check. An
order that is only waiting on the exchange counts too, because the exchange
sets funds aside for it. Confirm the activity was expected, wait until nothing
is left open and a later reconciliation is clean, then follow the re-arm
checklist. See [orders the app did not place](phase-1.5-risk-execution-portfolio-reconciliation.md#orders-the-app-did-not-place).

### An order is pending or unknown

1. Select **EMERGENCY STOP** if the system is not already halted.
2. Find the persisted client order ID on **Order history**: set **Status** to
   `unknown` or `pending_submit`, then open the order's lineage and copy its
   client order ID.
3. Query the broker for that same ID.
4. Update local state through the normal recovery path.
5. Never create a replacement order until the original is proven absent and the
   approved recovery procedure permits a new submission.

If the broker has no record of the order, follow the next section.

### An order the venue never received

An order can be saved as `pending_submit` and never sent if the process stops
between saving it and submitting it. Such a saved order stays unresolved; the
trading loop halts after five minutes, startup recovery halts on it, and a
re-arm would halt again while it remains pending. An administrator can close
it from the dashboard after the venue confirms it has no record and no fills.

If the pre-submit status lookup fails during the same call that created a new
order row, the app knows it has not called the venue's submit operation. It
closes that row as never sent, records an audit event with actor `system`, and
sends one warning alert through configured destinations. That loop reports a
broker error, but does not halt or change the kill switch. The next loop makes
a fresh signal and risk decision. A row that existed before the call, or any
order whose submission may have reached the venue, stays unresolved for
recovery and operator review.

If the lookup keeps failing, the app does not keep closing orders forever. The
third order in a row closed this way halts trading, and the halt alert names
the count and the failure. Every one of those orders is already closed, so
nothing needs closing: check that the venue is reachable, then re-arm. A lookup
that works in between starts the count over.

1. Sign in as an administrator. On the dashboard, **Orders waiting on the
   venue** (below **Re-arm**) lists every saved order that is `pending_submit`
   or `unknown`, with its client order ID. Operators do not see it.
2. Enter the **reason for closing this order**, for example an incident or
   ticket reference and who approved. It is required and limited to 500
   characters. Do not include tokens, account identifiers, addresses, or
   provider payloads: the server removes secrets, URLs, email addresses, and
   long identifiers before it saves the reason.
3. Select **Close order: never received by the venue**.
4. At that moment the app asks the venue for the order by its client order ID,
   then for its fills. It closes the order only when the venue answers that it
   has no record of the order and returns no fill. The result page shows what
   the venue answered and whether anything changed.

A closed order ends as `canceled`, which is also how an order the broker
canceled ends. History tells them apart: under **Order history**, the order is
marked **Closed by an administrator: never received by the venue** with who
closed it, when, the reason, and what the venue answered, and the audit event
(`order_closed_never_received`) is under **System events**. The order keeps its
signal, strategy version, and risk decision.

The app refuses to close the order, and changes nothing, when:

- the venue has any record of the order, or reports any fill for it. The order
  was received, so let startup recovery or reconciliation resolve it;
- a lookup fails in any way, including a `5xx` error, a timeout, or an
  authentication error. Only a definite "not found" answer counts. Try again
  when the venue responds;
- the order is no longer pending or unknown;
- the closure cannot be saved to the audit history (`503`); or
- the caller is not an administrator (`403`) or is not signed in (`401`).

A successful close and a refused attempt each send an alert to the configured
destinations.

Closing an order never submits it again, never cancels or changes anything at
the venue, and never creates a replacement. A later order comes from a fresh
strategy signal and a fresh risk decision. Closing also does not re-arm: the
kill switch is unchanged, and the re-arm checklist is still required. Once the
dashboard lists no order waiting on the venue, the checklist's third item can be
confirmed.

### The dashboard is unavailable

1. Ask an authorized engineer to check the liveness endpoint at `/health`.
2. If liveness also fails, keep the independent environment/file kill-switch
   actuation at `paused` or `halted` and escalate.
3. Do not restart repeatedly. A restart can trigger recovery and must be
   reviewed afterward.

### Information to collect safely

Provide the engineer with:

- UTC date and time;
- expected mode and displayed mode;
- kill-switch, recovery, connectivity, and reconciliation statuses;
- the visible, redacted error or alert text;
- the reviewed application commit or release identifier; and
- the steps immediately before the problem.

Do not provide tokens, keys, private URLs, hostnames, IP addresses, account
identifiers, database dumps, or unredacted provider responses in public records.

# Part II: Engineering and agent guide

## Engineering orientation

The service is implemented in Python 3.12 with FastAPI, SQLAlchemy, Alembic,
PostgreSQL, Pydantic, and provider-neutral broker contracts. A .NET component
integrates over authenticated HTTP/JSON; it does not need to embed Python or
reimplement trading rules.

An AI agent must read `AGENTS.md` before claiming or editing a card. The guide
requires repository inspection, an uncontested claim, an isolated worktree,
exact-path staging, fail-closed behavior, and separate reporting for local
validation, exact-head remote CI, and owner-run provider evidence.

## Repository map

| Path | Responsibility |
| --- | --- |
| `core/` | Shared models, modes, identifiers, startup guards, logging, and resilience. |
| `brokers/` | Provider-neutral interface, simulator, Gemini Sandbox, and Coinbase adapters. |
| `data/` | Coinbase public market data, storage, normalization, validation, replay recording, and gap fill. |
| `strategy/` | Pure strategy and research code. It must not import brokers, databases, HTTP, or network code. |
| `risk/` | Ordered, fail-closed risk gates and the persistent kill switch. |
| `execution/` | Audit persistence, idempotent submission, ambiguity resolution, and fills. |
| `portfolio/` | Broker-authoritative state, reconciliation, snapshots, and scheduling. |
| `api/` | Authenticated operator views, controls, bounded read-only history, health, and alert routing. |
| `app/` | Composition root, startup recovery, broker selection, trading loop, and replay runner. |
| `db/`, `alembic/` | SQLAlchemy persistence and schema migrations. |
| `deploy/` | Docker/VPS startup, drills, encrypted backup, and restore verification. |
| `probes/` | Owner-run exchange checks: the Gemini Sandbox lifecycle, Coinbase read-only reconciliation, and the Coinbase sandbox capture. CI tests them only against fixtures. |
| `tests/` | Unit, contract, integration, failure-path, and controlled process-kill evidence. |

Provider-specific behavior stays in `brokers/`. Do not bend strategy, risk,
portfolio, or reconciliation contracts around one venue.

## Core safety invariants

- Use `Decimal` for prices, quantities, fees, balances, and P/L.
- Keep mode values exactly `backtest`, `replay`, `paper`, and `live`.
- Strategies return signals and cannot submit orders or access external systems.
- Execution accepts an approved `RiskApproval`, not a raw signal.
- Persist the unique `client_order_id` before calling a broker.
- Resolve ambiguous submissions by client order ID before any retry.
- Missing or stale risk input refuses the order.
- The broker is authoritative during reconciliation.
- Divergence alerts the operator and trips the configured safety response before
  new entries.
- `HALTED` persists and requires an authenticated administrator re-arm with the
  complete checklist and a cause-and-approval reference. Operator stops only
  raise severity; a pause never lowers a halt.
- Every kill-switch transition is saved to `system_events`. A stop takes effect
  even when the database is down and is saved once it returns; a re-arm is saved
  first and refused if it cannot be.
- Automated tests never contact an exchange or place a real order.
- Credentials may never allow withdrawals or transfers.

## Mode, broker, and credential matrix

| Mode | `CREDENTIAL_SCOPE` | `BROKER_PROVIDER` | Result |
| --- | --- | --- | --- |
| `backtest` | `none` or `view` | empty | Valid; no provider broker. |
| `replay` | `none` or `view` | empty | Valid; no provider broker. |
| `paper` | `none` | empty | Valid credential-free service. `SimulatedBroker` requires deliberate test or application composition; the default entry point does not construct it. |
| `paper` | `view` | `gemini-sandbox` | Valid with Gemini Sandbox credentials under the current startup contract. |
| `paper` | any | `coinbase` | Refused. Paper orders must not reconcile against a Coinbase production account. |
| `live` | `trade` | `coinbase` | Structurally allowed only with exact confirmation and a Coinbase key proven able to trade and unable to transfer. Still not authorized by the current project phase. |

The effective code currently rejects `CREDENTIAL_SCOPE=trade` in all non-live
modes. If Gemini Sandbox needs order-capable credentials, preserve the public
configuration label required by the startup contract and treat provider scope
separately; do not weaken the guard without an explicit product and safety
decision.

## Local setup

### Prerequisites

- Python 3.12 or newer;
- Git;
- Docker with Docker Compose for the application database path; and
- PowerShell on Windows for the examples below.

### Install for development

From an isolated worktree:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Create a local `.env` only when the approved workflow requires it, protect it
with local secret-management controls, and never commit it. Git ignoring the
file does not make its contents safe.

### Safest service start

The Compose path starts PostgreSQL, runs Alembic migrations in the app
entrypoint, persists database and kill-switch volumes, and defaults to
`TRADING_MODE=backtest`, `CREDENTIAL_SCOPE=none`, and no broker:

```powershell
docker compose up --build
```

Check liveness without exposing state:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Stop containers without deleting state:

```powershell
docker compose stop
```

Never use `docker compose down -v` against an environment whose database or
kill-switch volumes matter.

### Direct Python start

`python -m app` starts the API on port 8000 after loading startup settings and
running startup recovery. It expects a reachable database whose migrations are
current. Prefer Compose unless you deliberately manage PostgreSQL and Alembic
yourself.

## Configuration reference

| Variable | Default | Contract |
| --- | --- | --- |
| `TRADING_MODE` | `backtest` | One of `backtest`, `replay`, `paper`, or `live`. |
| `CREDENTIAL_SCOPE` | `none` | One of `none`, `view`, or `trade`. `trade` is refused outside `live`. |
| `LIVE_CONFIRMATION` | empty | Must equal `I_UNDERSTAND_LIVE_TRADING` in `live`; necessary but not sufficient authorization. |
| `DATABASE_URL` | local PostgreSQL URL | SQLAlchemy URL. Production uses PostgreSQL; SQLite is limited to tests and portable backtest/replay archives. |
| `APP_ENV` | unset | Only `test` or `archive` changes behavior: either permits a SQLite `DATABASE_URL` outside `backtest` and `replay`, for isolated tests and portable archives. Leave it unset in deployments. |
| `LOG_LEVEL` | `INFO` | Application log level. Secret-bearing fields are redacted by the logging layer. |
| `BROKER_PROVIDER` | empty | Empty, `gemini-sandbox`, or `coinbase`, subject to the mode matrix. |
| `GEMINI_API_KEY` | empty | Required with `BROKER_PROVIDER=gemini-sandbox`. Secret. |
| `GEMINI_API_SECRET` | empty | Required with `BROKER_PROVIDER=gemini-sandbox`. Secret. |
| `COINBASE_API_KEY` | empty | Required with `BROKER_PROVIDER=coinbase`. Secret. |
| `COINBASE_PRIVATE_KEY` | empty | Required with `BROKER_PROVIDER=coinbase`. Secret; may contain line breaks. |
| `OPERATOR_TOKEN` | empty | Required for authenticated operator access. Also signs session cookies. |
| `OPERATOR_ADMIN_TOKEN` | empty | Optional locally, required by the VPS Compose override. When present, only this token may re-arm; when absent, the operator token receives administrator role. |
| `OPERATOR_COOKIE_SECURE` | `0` | Set to `1` behind production HTTPS so the browser sends the session cookie only over secure transport. |
| `KILL_SWITCH_FILE` | unset | Persistent JSON state path; Compose uses `/var/lib/trader/kill-switch.json`. |
| `TRADING_KILL_SWITCH` | empty | Independent external request for `paused` or `halted`. `running` never re-arms. Invalid content halts. |
| `STRATEGY_VERSION` | `unknown` | Version displayed on the operator surface. |
| `RECONCILE_INTERVAL_SECONDS` | `300` | Broker reconciliation interval; must be greater than 0 and no more than 3600. |
| `PAPER_RUNTIME_ENABLED` | `0` (`1` in the VPS override) | Starts the live Coinbase market-data runtime only in `paper` mode and only with a configured broker. |
| `PAPER_SYMBOLS` | `BTC-USD` | One to ten comma-separated Coinbase `*-USD` products. |
| `PAPER_STRATEGY_FAST_BARS` / `PAPER_STRATEGY_SLOW_BARS` | `3` / `8` | Moving-average windows; slow must exceed fast. |
| `PAPER_HISTORY_BARS` | `50` | Closed five-minute bars retained in each strategy state; between `slow + 1` and `300`. |
| `PAPER_ORDER_QUANTITY` | `0.0001` | Positive base-asset quantity requested by the reference strategy. |
| `PAPER_MIN_NOTIONAL` | `1` | Positive minimum notional supplied to the exchange-constraint risk gate. |
| `PAPER_ESTIMATED_SLIPPAGE` | `0.005` | Non-negative estimate no greater than the 1% default risk limit. |
| `PAPER_COOLDOWN_SECONDS` | `300` | Non-negative per-symbol order cooldown. |
| `WATCH_FEED_ENABLED` | `0` | Starts the store-only feed that collects candles for watchlist coins that are not in `PAPER_SYMBOLS`. Public market data only; it never trades. |
| `WATCH_FEED_RETENTION_DAYS` | `30` | Days of watch-only candles kept, from 30 to 365. Only watched coins outside `PAPER_SYMBOLS` are pruned. |
| `TRADING_KILL_SWITCH_FILE` | unset | Optional plain-text external flag read on every paper cycle; `paused` and `halted` can tighten state, while `running` cannot re-arm. |
| `LOSS_STATE_FILE` | next to `KILL_SWITCH_FILE` | Persists opening/peak equity so daily-loss and drawdown checks survive restart. |
| `ALERT_NTFY_TOPIC_URL` | empty | Full HTTPS ntfy topic URL for phone push. Treat a private topic URL as a secret. |
| `ALERT_NTFY_TOKEN` | empty | Optional ntfy bearer token. Secret. |
| `ALERT_SMTP_HOST` / `ALERT_SMTP_PORT` | empty / `587` | SMTP server; host, from-address, and to-address are required together. |
| `ALERT_SMTP_USERNAME` / `ALERT_SMTP_PASSWORD` | empty | Optional SMTP credentials; configure both or neither. Secret. |
| `ALERT_SMTP_STARTTLS` | `1` | Require STARTTLS after connecting. |
| `ALERT_EMAIL_FROM` / `ALERT_EMAIL_TO` | empty | Sender and operator recipient for email alerts. Recipient data stays out of logs and repository files. |
| `ALERT_TIMEOUT_SECONDS` | `10` | Positive network timeout for each configured alert sink. |

Startup must fail rather than silently correcting an invalid mode, scope,
provider, credential, live confirmation, or reconciliation interval.

## HTTP interface

| Method and path | Authentication | Purpose |
| --- | --- | --- |
| `GET /health` | None | Liveness only; returns `{"status":"ok"}`. |
| `GET /health/detail` | Operator | Application, broker, and strategy health. |
| `GET /health/strategies` | Operator | Strategy heartbeat list. |
| `GET /operator/login` | None | Browser login form. |
| `POST /operator/login` | Token in form body | Establishes an HttpOnly, SameSite=Strict session lasting up to eight hours. |
| `POST /operator/logout` | None | Ends the session: revokes it on the server and clears the cookie. |
| `GET /operator` | Operator | Server-rendered dashboard. |
| `GET /operator/fragment` | Operator | The dashboard body without the page shell or styles. The page does not refresh itself. |
| `GET /operator/state` | Operator | Full operator snapshot as JSON. |
| `GET /operator/kill-switch` | Operator | Current kill-switch state. |
| `POST /operator/pause` | Operator | Persist `paused` from `running`. Never lowers `halted`; returns the unchanged state instead. |
| `POST /operator/emergency-stop` | Operator | Persist `halted`. Unchanged if already halted. |
| `GET /operator/rearm` | Administrator | Re-arm review page: active warnings, recent kill-switch changes, and the checklist form. |
| `POST /operator/rearm` | Administrator | Persist `running` after the review. Requires every checklist item and a reason. The only route that lowers the kill switch. |
| `GET /operator/watchlist` | Operator | The saved watchlist with each coin's feed state, as a page or JSON. Takes no parameters. |
| `GET /operator/markets` | Operator | Server-drawn one-to-nine-coin grid from stored candles. Parameters: `c` (shareable comma-separated `*-USD` symbols), `window`, and `interval`; invalid, duplicate, or oversized layouts are refused. |
| `POST /operator/watchlist/add` | Operator | Form or JSON `symbol`. Refuses unknown, delisted, or trading-disabled products, duplicates, and a tenth coin. |
| `POST /operator/watchlist/remove` | Operator | Form or JSON `symbol`. |
| `POST /operator/watchlist/reorder` | Operator | JSON `order` (every coin once), or `symbol` with `direction` `up` or `down`. |
| `GET /operator/markets/candles` | Operator | Stored candles as bars for charts, as JSON only. Parameters: `symbols` (up to nine, comma-separated, default the watchlist), `window` (`24h`, `7d`, `30d`, `90d`), `interval` (`15m`, `1h`, `6h`, `1d`; default the finest that keeps a series within 300 points). Each coin returns its bars, gaps, source, and one freshness state: `fresh`, `stale`, `not_collected`, or `unavailable`. Refuses anything beyond the caps with `400`. |
| `GET /operator/history/orders` | Operator | Orders with their fills and lineage. Filters: `symbol`, `status`, `strategy_version`, `client_order_id`, `correlation_id`. |
| `GET /operator/history/orders/{client_order_id}` | Operator | One order's lineage: signal, risk decision, order, fills, and any gaps. |
| `GET /operator/history/signals` | Operator | Signals with their decision and order. Filters: `symbol`, `strategy_version`. |
| `GET /operator/history/risk-decisions` | Operator | Approvals and refusals. Filters: `outcome`, `failed_gate`, `symbol`, `strategy_version`, `correlation_id`. |
| `GET /operator/history/risk` | Operator | Latest refusal, refusals by ordered gate, and kill-switch history. |
| `GET /operator/history/discrepancies` | Operator | Reconciliation differences by field name. Filter: `entity_type`. |
| `GET /operator/history/events` | Operator | System events. Filter: `event_type`. |
| `GET /operator/history/trends` | Operator | Counts per time bucket: reconciliation runs, discrepancies by type, and refusals by gate, plus the charts not started yet. Parameter: `window`. |
| `GET /operator/research` | Operator | Approved dataset catalog, bounded run form, recent jobs, and comparison links. |
| `POST /operator/research/runs` | Operator | Queue a backtest or simulator replay. No provider writes or mode changes. |
| `GET /operator/research/runs/{run_id}` | Operator | Read one bounded research job and its report. |
| `GET /operator/research/compare?run=...` | Operator | Compare up to five completed versioned runs. |
| `GET /operator/research/runs/{run_id}/export` | Operator | Download a completed redacted JSON report. |

For programmatic access, send the token in the `x-operator-token` header. The
server refuses any request with a `token` query parameter (`400`). Cookie
authentication is intended for the browser, and header requests receive no
cookie. The current API does not implement a general trading-command endpoint.

A control returns its result as JSON unless the request accepts `text/html`:

```json
{"action": "pause", "state": "paused", "changed": true, "role": "operator", "at": "2026-09-27T21:00:00+00:00"}
```

`POST /operator/rearm` takes a form body, repeating `checklist` once per item,
or JSON:

```json
{
  "checklist": [
    "cause_documented",
    "mode_and_scope_confirmed",
    "orders_resolved",
    "broker_state_confirmed",
    "recovery_and_reconciliation_clean",
    "inputs_current",
    "approval_obtained"
  ],
  "reason": "INC-42: sandbox check ended and reconciliation is clean; approved by the owner"
}
```

A refused re-arm returns `{"detail": {"errors": [...], "missing_checklist": [...], "state": "halted"}}`
with `422`, or `503` when the transition cannot be saved.

The history routes are GET only and return JSON unless the request accepts
`text/html`. Every list is bounded:

- **Window:** `window` is `1h`, `24h` (the default), `7d`, or `31d`; or send
  `since` and optionally `until` as ISO 8601 times (no offset means UTC). A
  window longer than 31 days is refused.
- **Page:** `limit` is 1 to 100 (default 25), newest first. A page with more
  rows returns `next_before`; send it back as `before` with the same `until`, so
  rows recorded later never shift the pages.
- **Refusals:** a wider window, a larger page, or an unknown, repeated, or
  malformed parameter returns `422` with
  `{"detail": {"status": "refused", "errors": [...]}}`, and nothing is read.

```json
{"kind": "orders", "status": "available", "query": {"since": "...", "until": "...", "window": "24h", "limit": 25, "max_limit": 100, "max_days": 31, "before": null, "filters": {}}, "total": 0, "count": 0, "rows": [], "next_before": null}
```

`"total": 0` means nothing was recorded in the window. A history that cannot be
read returns `503` with `{"detail": {"status": "unavailable", "reason": "..."}}`,
never an empty list. A lineage for an unknown client order ID returns `404`.
Responses carry identifiers, statuses, amounts, reasons, and times; provider
payloads stay in the database.

`/operator/history/trends` takes one parameter, `window`: `24h` (the default,
24 hourly buckets), `7d` (28 six-hour buckets), or `30d` (30 daily buckets).
Buckets align to UTC, and the last one is still in progress. Any other window,
or any other parameter, returns `422` and nothing is read. Each available chart
names its source and returns per-bucket counts; a chart with no producer returns
`"status": "not_started"` and its reason, never an empty series.

```json
{"kind": "trends", "status": "available", "query": {"window": "24h", "since": "...", "until": "...", "as_of": "...", "bucket_seconds": 3600, "buckets": 24, "max_days": 30, "in_progress_from": "..."}, "charts": [{"key": "reconciliation_runs", "status": "available", "source": {"table": "portfolio_snapshots", "where": "source = 'broker'", "time": "recorded_at"}, "series": [{"key": "completed", "counts": [12, 12, "..."], "total": 265}], "totals": ["..."], "total": 265}, {"key": "equity_pnl", "status": "not_started", "reason": "...", "blocked_by": ["#30"]}]}
```

A trends read that fails returns `503` with the same `unavailable` detail as the
history, never a chart of zeros.

FastAPI also exposes its generated API documentation by default. A production
reverse proxy should apply the deployment's access policy to `/docs`,
`/redoc`, and `/openapi.json`; their existence is not a substitute for the
maintained safety contracts in this manual and the repository documentation.

## .NET read-only integration example

Use a named or typed `HttpClient`, keep the token in an approved secret store,
and reuse the client. This example reads state; it does not change a control:

```csharp
using System.Net.Http.Json;
using System.Text.Json.Serialization;

public sealed class TraderOperatorClient(HttpClient httpClient)
{
    private readonly HttpClient _httpClient = httpClient;

    public async Task<OperatorSnapshot?> GetStateAsync(
        string operatorToken,
        CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, "operator/state");
        request.Headers.Add("x-operator-token", operatorToken);
        using var response = await _httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<OperatorSnapshot>(
            cancellationToken: cancellationToken);
    }
}

public sealed record OperatorSnapshot(
    [property: JsonPropertyName("application")] ApplicationState Application,
    [property: JsonPropertyName("trading")] TradingState Trading,
    [property: JsonPropertyName("connectivity")] ConnectivityState Connectivity,
    [property: JsonPropertyName("risk")] RiskState Risk,
    [property: JsonPropertyName("recovery")] RecoveryState Recovery);

public sealed record ApplicationState(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("heartbeat")] DateTimeOffset Heartbeat);

public sealed record TradingState(
    [property: JsonPropertyName("mode")] string Mode,
    [property: JsonPropertyName("credential_scope")] string CredentialScope,
    [property: JsonPropertyName("strategy_version")] string StrategyVersion);

public sealed record ConnectivityState(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("detail")] string Detail,
    [property: JsonPropertyName("checked_at")] DateTimeOffset? CheckedAt);

public sealed record RiskState(
    [property: JsonPropertyName("kill_switch")] string KillSwitch);

public sealed record RecoveryState(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("detail")] string Detail);
```

The JSON uses snake_case, so the example maps names explicitly. It intentionally
models only the top-level health and safety fields; extend it for portfolio,
orders, fills, signals, reconciliation, alerts, and errors. Treat unknown enum
values and missing required fields as a safe failure, not as `running` or
healthy.

For control calls, make the intent explicit in the method name, require a
separate authorization decision for re-arm, set timeouts, and audit the outcome.
Do not automatically re-arm after a successful health poll.

## Processing and safety flow

The intended trading path is:

```text
closed market data
  -> pure strategy signal
  -> persisted signal and ordered risk evaluation
  -> approved RiskApproval
  -> persisted PENDING_SUBMIT order and unique client_order_id
  -> broker submission
  -> status/fill persistence
  -> broker-authoritative portfolio reconciliation
```

At any uncertainty boundary, stop progressing forward:

- stale or missing market/risk input: refuse;
- order-persistence failure: do not call the broker;
- ambiguous submission: mark `UNKNOWN` and query before retry;
- database failure after a broker call: halt and recover;
- reconciliation difference: adopt the broker record, alert, and halt;
- unreadable or invalid external kill-switch flag: halt.

## Development validation

Run the repository's lightweight required gates from the isolated worktree:

```powershell
ruff format --check .
ruff check .
mypy core brokers data strategy risk execution portfolio api app db probes
pytest --cov=. --cov-report=term-missing --cov-fail-under=80
git diff --check
```

The coverage threshold is a safety baseline; do not lower it to make a change
green. Tests use deterministic fixtures, `SimulatedBroker`, temporary state,
and Gemini Sandbox only when explicitly owner-run. Required GitHub checks are
green only after they complete against the exact final pull-request head SHA.

Report evidence in three separate categories:

1. **Local validation:** commands and results from the isolated worktree.
2. **Remote CI:** completed required checks for the exact pull-request head.
3. **Owner-run verification:** credentialed provider checks, real soaks, or
   infrastructure drills, with secrets and account information redacted.

Passing unit tests or CI never proves live-provider behavior.

## Change protocol for AI agents and contributors

Before editing a card, inspect the repository, full issue, dependencies,
comments, claims, branches, worktrees, and open pull requests as specified in
`AGENTS.md`. Claim one eligible card, re-read for competing claims, and work in
a dedicated worktree based on refreshed `origin/main`.

During implementation:

- preserve unrelated files, branches, worktrees, stashes, and untracked work;
- stage only the claimed paths;
- keep public CI lightweight unless the card explicitly requires more;
- never use live credentials or publish sensitive evidence;
- cover refusal, stale-input, timeout, duplicate, and recovery paths; and
- never weaken a safety gate merely to make a check pass.

Open a draft pull request only after the claimed scope is implemented and
locally checked. Do not merge, release, deploy, or enable live trading unless
the card and the user explicitly authorize that next step.

## Deployment and recovery

The production-style deployment uses `docker-compose.yml` plus
`deploy/docker-compose.vps.yml`. Migrations run before the app process accepts
requests. PostgreSQL and kill-switch state live in named volumes.

Use the exact reviewed commit and follow
`docs/phase-1.8-deployment-backups-restore.md`. Infrastructure verification
uses `deploy/drill.sh` and the `Restore drill` workflow. Do not improvise
commands against the production database, and do not reboot a host without the
owner's immediate approval for that one interruption.

After a halt, `sh deploy/drill.sh diagnose` is the read-only way to see why. It
prints the kill-switch and recovery state, each pending or unknown order and
whether the venue knows it, the last stored discrepancies with their values and
deltas, and the projected-versus-broker balance for the latest fills, itemized as
`quantity × price`, fee, and rounding. It refuses to run in `live` mode or with a
trade-capable credential scope, changes nothing, and prints no secret, token,
host name, or address. The app's own log now names a divergence's field and
delta (and both values in `paper` only), a provider failure's status code,
endpoint path, and reason, and each order step once.

Nightly backups are encrypted before leaving the host. A backup is not proven
usable until `deploy/restore-verify-postgres.sh` restores it into a separate
scratch database and verifies the migration version and durable table counts.
Never restore a drill backup over the production database.

## Troubleshooting reference

| Symptom | Likely contract | Safe next step |
| --- | --- | --- |
| Service refuses non-live startup with trade scope | Non-live modes reject `CREDENTIAL_SCOPE=trade`. | Correct the scope; do not weaken the guard. |
| Live startup refuses explicit configuration | Confirmation, broker choice, declared scope, or Coinbase-reported permissions failed. | Keep live disabled; review every guard and provider permission. |
| `503 operator authentication is not configured` | `OPERATOR_TOKEN` is absent. | Configure it through approved secret handling and restart. |
| Operator gets `400` | The request carried a `token` query parameter. | Sign in through the form, or send the `x-operator-token` header. Treat a token that appeared in a URL as exposed and rotate it. |
| Operator gets `401` | Token/session is missing, invalid, expired, or signed out. | Use the login form or correct header; do not put the token in the URL. |
| Operator gets `403` on re-arm | Operator token lacks administrator role. | Obtain deliberate administrator authorization; do not bypass the route. |
| Re-arm returns `422` | A checklist item or the cause-and-approval reference is missing, or the reference exceeds 500 characters. | Complete the review; the kill switch is unchanged. |
| Re-arm returns `503` | The transition could not be saved to the database. | Leave the system stopped and restore the database first. |
| Startup recovery is `no_broker` | No broker is configured and no pending order exists. | Expected only for an intentionally credential-free environment. |
| Startup recovery halts | Pending order, provider outage, missing baseline, database read failure, or divergence. | Keep halted and reconcile against the broker. Run `sh deploy/drill.sh diagnose` to see which order, value, or call. |
| Trading halted and the log says only a divergence or a provider error | The broker is authoritative; a divergence or a failed call halts trading. | Run `sh deploy/drill.sh diagnose`; read the `reconciliation divergence` and `failed:` log lines for the field, delta, status, path, and reason. |
| Broker says unavailable but old balances remain visible | Last-known values are retained only for diagnosis. | Do not treat them as current; pause or halt as risk requires. |
| Reconciliation interval is rejected | Value is non-numeric, zero/negative, or above 3600 seconds. | Set a valid bounded interval. |
| Kill switch halts after reading a flag | Flag was `halted`, invalid, or unreadable. | Fix the external flag source, investigate, then manually re-arm. |
| `/health` is OK but dashboard is degraded | Liveness does not include broker or strategy health. | Investigate detailed authenticated state. |
| Strategy remains `unknown` | The paper runtime is disabled, lacks a broker, or has not started. | Check `runtime` in `/operator/state`, configuration, startup logs, and the kill switch before re-arming. |

## Related documents

- [`README.md`](../README.md) — project status and quick start.
- [`AGENTS.md`](../AGENTS.md) — mandatory repository and concurrent-work rules.
- [`docs/foundation.md`](foundation.md) — foundation contracts.
- [`docs/phase-1.5-risk-execution-portfolio-reconciliation.md`](phase-1.5-risk-execution-portfolio-reconciliation.md) — risk, execution, and reconciliation contracts, including orders the app did not place.
- [`docs/phase-1.7-operator-surface.md`](phase-1.7-operator-surface.md) — operator authentication and state contract.
- [`docs/owner-run-exchange-checks.md`](owner-run-exchange-checks.md) — owner-run exchange checks and their recorded results.
- [`docs/phase-1-gate-acceptance.md`](phase-1-gate-acceptance.md) — acceptance evidence and remaining owner-run gaps.
- [`docs/phase-1.8-deployment-backups-restore.md`](phase-1.8-deployment-backups-restore.md) — deployment, backup, restore, and restart runbook.
- [`docs/phase-1.8-vps-drill-checklist.md`](phase-1.8-vps-drill-checklist.md) — controlled infrastructure evidence procedure.
- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting and sensitive-data boundaries.

## Manual maintenance checklist

Update this manual whenever a release changes any of the following:

- supported modes, brokers, credentials, or startup guards;
- dashboard fields, authentication, routes, or control roles;
- trading-loop, strategy-heartbeat, P/L, or alert runtime wiring;
- order recovery or reconciliation semantics;
- environment variables or deployment topology;
- backup, restore, or incident procedures; or
- owner-run evidence and live-activation status.

Verify the manual against current source and tests rather than copying planned
behavior from a roadmap.

After editing this Markdown source, regenerate the styled manual with
`python tools/build_user_manual.py` and commit the resulting
`docs/user-manual.html` alongside the source change.
