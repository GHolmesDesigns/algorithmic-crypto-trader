# UI design handoff: Algorithmic Crypto Trader

- **Prepared:** 2026-09-27
- **Current merged baseline:** `origin/main` at `15e168a`
- **Audience:** the agent responsible for completing the product UI design
- **Scope:** design and information architecture, not authorization to enable live trading or add provider writes

> **Status since this report (2026-09-27, `main` at `e9763b7`).** The report below is kept as prepared. Where it conflicts with this note, this note wins.
>
> - **#54 merged.** In `paper` mode, a configured broker plus `PAPER_RUNTIME_ENABLED=1` (`0` by default, `1` in the VPS override) now starts the Coinbase market-data runtime and drives the trading cycle.
>   - Operator state gains a `runtime` block with seven statuses: `not_started`, `running`, `degraded`, `failed`, `halted`, `stopped`, `disabled`.
>   - ntfy phone-push and SMTP email sinks are built from the environment.
>   - This supersedes the "not started by default" and "no concrete sinks" statements in §5.3, §5.7, and §5.9, and the "Draft" status in §9.1 and §16.
> - **#56 and #58 merged:** fixes to Coinbase market data, with no change to the operator state.
> - **#55 / PR #57 merged.** An operator PAUSE no longer lowers a halt, and only the administrator re-arm lowers the kill switch. The `POST /operator/pause` and `POST /operator/emergency-stop` rows in §5.9 now leave a stricter state unchanged.
> - **#59 / PR #67 merged:** the styled, accessible command center, built from `/operator/state` with local CSS and no JavaScript. It supersedes the §4 audit of the unstyled page.
>   - All merged fields are rendered, including the seven runtime statuses.
>   - Refresh is a working link; the dead HTMX attributes are gone.
>   - PAUSE is hidden while halted, and RE-ARM moved to Risk & safety.
> - **#60 merged:** safety controls end on a server-rendered result page, and **Sign out** (`POST /operator/logout`) ends the session.
>   - RE-ARM is now the administrator re-arm review at `GET /operator/rearm`. It repeats the active warnings, and the server enforces the seven checklist items and a redacted cause-and-approval reference.
>   - Kill-switch transitions are saved to `system_events` and returned as `risk.transitions` in `/operator/state`; the safety bar shows the last change as the cause.
>   - Query-string tokens are refused with `400`. This supersedes the §4.1 "no sign-out control" and the §5.9 query-token compatibility statements.
> - **#61 merged:** bounded, read-only history under `/operator/history/*`, as JSON and HTML. Orders expand into their lineage (signal, risk decision, order, fills); signals, risk decisions, discrepancies, and system events are filterable; **Risk & safety** shows the latest refusal, refusals by the 17 ordered gates, and kill-switch history.
>   - Every list caps its window at 31 days and its page at 100 rows, and refuses anything wider.
>   - "0 recorded" and "not available" render differently. This supersedes the §5.10 and §8.2 statements that persisted records lack browser read APIs, and the §8.4 statement that gate views need new read models.
> - **#62 merged:** **Trends** at `/operator/history/trends`, as JSON and HTML. It shows reconciliation runs, discrepancies by type, and risk refusals by gate over 24 hours, 7 days, or 30 days, counted by the database from persisted rows.
>   - Charts are server-drawn SVG in one neutral ink, each naming its source table and followed by a table of the same counts. There is no JavaScript and no CDN.
>   - The window is capped at 30 days and 30 bars on the server.
>   - Uptime and freshness, and equity and day P/L, render as **Not started** with the reason: no persisted producer exists, and P/L waits on #30.
>   - This delivers the §9.3 "reconciliation/discrepancy trend" and "risk-rejection histogram". The "uptime and freshness trends" and "equity and day-P/L chart" still need producers.
> - **The §10 iterations are now carded:**
>   - Iteration A → #59 (command center, merged) and #60 (safety controls, sign-out, enforced re-arm review);
>   - Iteration B → #61 (bounded history) and #62 (trends);
>   - Iteration C → #64;
>   - Iteration D → #63.
>
>   Delivery 5 (controlled-live support) stays with #13 until it is separately authorized.
> - **Roadmap Rev. C (2026-09-29) adds Phase 4: multi-coin market charts** (planning document §9; cards #87–#91). It is **Backlog**.
>   - A Markets page at `/operator/markets` will show up to nine coins the operator chooses in the app. Its charts are drawn from the app's own stored Coinbase candles, never from TradingView's hosted widgets.
>   - Every tile is server-drawn SVG with a table alternative. A pinned, vendored copy of TradingView's open-source Lightweight Charts library, served from the app's own origin, may add zoom, pan, and crosshair on that page only.
>   - This is the one exception to "no JavaScript" on the operator surface. The other pages, and every safety control, stay JavaScript-free, and nothing loads from a CDN.

## 1. How to read this report

This report uses four status labels so design concepts do not get mistaken for working product behavior:

| Label | Meaning |
| --- | --- |
| **Merged** | Present on the current `origin/main` baseline. |
| **Draft** | Implemented on an open pull request, but not part of the merged product yet. |
| **Backlog** | Recorded in an open issue or the approved planning document. |
| **Proposed** | Feasible from the existing architecture, but it needs a card and acceptance criteria before implementation. |

The UI must always distinguish current, last-known, unavailable, unknown, pending, and failed data. A blank value is not equivalent to zero, healthy, or safe.

## 2. Product brief

Algorithmic Crypto Trader is a personal, safety-first trading system. It supports historical research, deterministic replay, simulated execution, Gemini Sandbox rehearsal, and a separately guarded Coinbase production path. The product is not a retail trading terminal and should not encourage impulsive order entry.

The primary UI is an operator command center. Its job is to answer these questions quickly and truthfully:

1. What mode and environment is this instance using?
2. Is the system allowed to act, paused, or halted?
3. Are market data, broker state, strategy activity, persistence, and reconciliation current?
4. What signals, risk decisions, orders, fills, alerts, and discrepancies occurred?
5. Is any information stale, unknown, or based on a last-known value?
6. What safe operator action is available now?

The design should feel calm, technical, and operational. It should optimize for anomaly detection and safe intervention rather than excitement, price speculation, or trade frequency.

## 3. Users and permissions

### 3.1 Operator

The operator may be non-technical. They need plain-language status, a reliable routine-check path, and immediate access to **PAUSE** and **EMERGENCY STOP**.

Merged permissions:

- sign in with `OPERATOR_TOKEN`;
- read the dashboard and authenticated health/state endpoints;
- pause the persistent kill switch;
- halt the persistent kill switch;
- view broker-authoritative balances and positions when available;
- view orders, fills, signals, strategies, errors, alerts, recovery, and reconciliation state exposed by the API.

### 3.2 Administrator

An administrator has every operator permission and may manually re-arm the system. When `OPERATOR_ADMIN_TOKEN` is configured, the ordinary operator cannot see or use **RE-ARM**.

Re-arm only changes the kill switch to `running`. It does not repair market data, a broker, persistence, a strategy, a discrepancy, or an unresolved order. The UI must communicate that distinction.

### 3.3 Engineer or AI agent

An engineer or agent uses the authenticated JSON endpoints, logs, database records, tests, deployment tools, and owner-run probes. The visual UI may expose safe diagnostics, but it must never render secrets, raw provider payloads, credential material, private host details, or live operational logs.

## 4. Current UI implementation audit

### 4.1 Merged browser surface

The current browser surface is intentionally minimal:

- `GET /operator/login` renders an unstyled token form.
- A successful form login sets an HttpOnly, SameSite=Strict session cookie for up to eight hours.
- `GET /operator` renders a single server-side Jinja page.
- `GET /operator/fragment` renders the state fragment.
- The page declares a 15-second HTMX refresh, but it does not load the HTMX library. Refresh is therefore manual in the merged baseline.
- There is no sign-out control.
- There is no application stylesheet, responsive layout, navigation, charting, filtering, pagination, or progressive disclosure.
- The dashboard uses semantic headings, paragraphs, lists, forms, and buttons, so its core controls work without JavaScript.
- Pause, emergency stop, and re-arm return a simple HTML confirmation page when used from the browser.

### 4.2 What the merged HTML currently renders

- application status;
- trading mode;
- strategy version;
- broker connectivity status and detail;
- kill-switch state;
- startup recovery status and detail;
- portfolio status and detail;
- P/L status and detail;
- balances;
- positions;
- counts and basic rows for orders, fills, and signals;
- strategy heartbeat rows;
- role-appropriate controls;
- errors, when present;
- alerts, when present.

### 4.3 State available in JSON but not rendered in merged HTML

The authenticated `GET /operator/state` payload already exposes additional designable information:

- application heartbeat timestamp;
- credential scope;
- broker `checked_at` timestamp;
- complete reconciliation status;
- balance and position `as_of` timestamps;
- full nested order-request fields;
- order created/updated times, quantities, order type, limit price, filled quantity, and average fill price;
- complete signal identifiers, correlation identifiers, quantities, and times;
- fill side, fee, fee asset, and occurrence time;
- strategy `last_seen` timestamps;
- alert delivery results and configured alert destinations;
- snapshot `updated_at` timestamp.

These fields are the highest-value inputs for the first UI iteration because they require little or no new backend capability.

### 4.4 Existing visual system available for reuse

The browser-ready user manual already defines a visual system in `docs/user-manual.css`. It is not connected to the operator app, but it supplies a credible starting point:

- warm neutral surfaces;
- a dark navigation rail;
- muted gold accent;
- green, amber, and red operational states;
- Archivo-style display typography, Source Serif-style reading typography, and a monospace data face;
- light and dark color schemes;
- visible focus styles;
- restrained shadows and compact navigation.

The operator UI may adapt these tokens for product consistency, but must use system fallbacks or deliberately package fonts rather than assuming remote font availability.

## 5. Complete merged capability inventory

This section inventories the full current codebase, including capabilities that are not yet visualized.

### 5.1 Modes, startup guards, and credentials

- Four exact modes: `backtest`, `replay`, `paper`, and `live`.
- Credential scopes: `none`, `view`, and `trade`.
- Unsafe mode/scope combinations fail before service initialization.
- Live mode requires the exact confirmation string, `BROKER_PROVIDER=coinbase`, a trade-capable key, and a provider-reported inability to transfer funds.
- Paper mode may use Gemini Sandbox; Coinbase is refused outside live mode.
- Gemini Sandbox rejects any non-`*.sandbox.gemini.com` endpoint at construction.
- An empty provider supports credential-free development, backtest, replay, and limited paper composition.
- The startup banner records mode and declared credential scope without printing secrets.

### 5.2 Domain and data model

Frozen Pydantic models exist for:

- candles;
- quotes;
- balances;
- positions;
- signals;
- order requests;
- orders;
- fills;
- market state;
- risk approvals.

Money, quantities, prices, fees, balances, and P/L use `Decimal`. Models validate candle time/OHLC consistency, crossed quotes, positive quantities, and limit-price requirements.

Order states are:

- `pending_submit`;
- `unknown`;
- `open`;
- `partially_filled`;
- `filled`;
- `canceled`;
- `rejected`.

Kill-switch states are:

- `running`;
- `paused`;
- `halted`.

### 5.3 Market data

- Public Coinbase historical candle client.
- Resumable, idempotent candle backfill.
- Coinbase WebSocket ingestion with ticker, heartbeat, reconnect backoff, and closed five-minute candle aggregation.
- REST gap fill after disconnect.
- Canonical candle and quote normalization with source, market time, and ingestion/receipt time.
- Quote freshness and age checks.
- Raw JSONL stream recording and reading for deterministic replay.
- In-memory and SQLAlchemy candle stores.
- Validation for duplicate bars, gaps, timestamp order, OHLC consistency, negative volume, stale quotes, and suspicious outliers.
- Failure paths for disconnects, duplicates, and stale data are covered by tests.

Important merged limitation: the default `app.main` process does not start the market-data ingestor.

### 5.4 Research, strategy, backtest, and replay

- Pure strategy boundary: strategy code returns signals and cannot import broker, database, HTTP, or network modules.
- Reference `AlwaysBuyStrategy` for known-answer validation.
- Reference moving-average crossover strategy.
- Event-driven backtester using closed bars.
- Configurable maker/taker fees, spread, slippage, fee asset, and partial-fill ratio.
- No-look-ahead guard and a canary test proving why it matters.
- Walk-forward train/test windows with an optional sealed holdout.
- Reports include data source, window, symbols, granularity, cost assumptions, trade count, exposure, return distribution, drawdown, drawdown duration, yearly/regime results, strategy version/hash, equity, return, and trades.
- Replay runner drives the strategy through the real risk and execution flow against a simulator and returns outcomes, fills, final equity, and audit data.
- Backtest/replay parity is tested on multiple windows, including high volatility.

There is no merged research or backtest UI. These capabilities currently exist as Python code and testable reports.

### 5.5 Broker abstraction and venue adapters

The shared `BrokerInterface` covers:

- provider/environment capabilities;
- streaming and historical-candle support flags;
- order types;
- price and quantity increments;
- quote staleness limits;
- balances;
- positions;
- quotes;
- order submission;
- order lookup by client order ID;
- fills.

`SimulatedBroker` supports:

- market fills at the far side plus configured slippage;
- limit-order matching;
- deterministic partial fills;
- balance and position accounting;
- idempotent client order IDs;
- injected reject, timeout, duplicate-acknowledgement, out-of-order-fill, rate-limit, and unavailability faults.

`GeminiBroker` supports Sandbox-only:

- authenticated balances and positions;
- quote reads;
- submit, status, fills, cancel, edit/cancel-replace, and polling;
- bounded market orders represented as immediate-or-cancel limits;
- partial fills, rejection, timeout recovery, and insufficient-funds handling;
- strict sandbox host enforcement.

`CoinbaseBroker` supports:

- public quotes, products, and candles;
- authenticated balances and positions;
- order submission, lookup, fills, cancel, edit/cancel-replace, and polling;
- authenticated user-order WebSocket events with polling fallback;
- JWT issuance/refresh for REST and WebSocket;
- pagination and documented error/status mapping;
- key-permission inspection;
- bounded 429 retry and circuit-breaker behavior;
- ambiguous submission recovery by client order ID.

The Coinbase adapter's presence is not authorization to place live orders.

### 5.6 Risk system

The risk engine evaluates ordered, fail-closed gates. Missing required input is a refusal, never a pass. Current gates cover:

1. kill switch;
2. operator pause and trading window;
3. broker health;
4. stale or future quote;
5. abnormal volatility;
6. expected/reference price divergence;
7. duplicate signal/order prevention;
8. symbol cooldown;
9. maximum open positions;
10. maximum trade notional;
11. per-symbol position and no unsupported shorting;
12. aggregate allocation;
13. minimum cash reserve;
14. daily loss;
15. drawdown;
16. estimated slippage;
17. exchange quantity, price, and notional constraints.

The kill switch persists across restarts. Dashboard/API controls and external file/environment signals may tighten state. External signals can pause or halt, but can never re-arm.

### 5.7 Trading cycle and execution

- Strategy output is recorded before risk evaluation.
- Execution accepts only a matching, approved `RiskApproval`, never a raw signal.
- A deterministic `client_order_id` is assigned from signal and order terms.
- The order is persisted before provider submission.
- Persistence failure prevents submission and halts unaudited trading.
- Ambiguous submission becomes `unknown` and is resolved by client order ID before any retry.
- Terminal order state is not accepted until authoritative fills are persisted.
- Duplicate, stale, timeout, provider-unavailable, pending, and failure paths block unsafe new entries.
- Trading and scheduled reconciliation share a lock so they cannot race.
- Audit lineage connects signal, strategy version, risk decision, order, and fills.
- Daily-loss and drawdown state can survive restart.
- Sell decisions may reduce risk without being trapped by buy-side exposure limits, but short selling is refused.

Important merged limitation: the `TradingCycle` exists and is extensively tested, but the default service does not continuously drive it from live market data.

### 5.8 Portfolio, reconciliation, and startup recovery

- Portfolio state includes orders, fills, positions, and balances.
- SQL persistence stores broker snapshots in atomic batches, plus equity and discrepancy records.
- The broker is authoritative during reconciliation.
- Quantity, balance, order, fill, and known-cost-basis differences may produce discrepancies.
- Divergence persists evidence, adopts broker state, alerts, and trips the configured safety response before new entries.
- Scheduled reconciliation tracks total, clean, diverged, and unavailable runs; last run/result/discrepancy count are available in JSON.
- Scheduled reconciliation continues operating through transient broker outages while trading remains fail-closed.
- Startup recovery reads persisted open/unknown orders, resolves them by client order ID, reloads portfolio state, and reconciles before accepting HTTP traffic.
- Missing baselines, unreadable persistence, unresolved orders, unavailable brokers, or divergence halt the system.
- Process-kill and database-outage paths are covered by controlled tests.

Model note: a venue position with no known cost basis has `average_price = null`; `0` is a real price (an airdrop). Issue `#30` made that change, so cost basis may now be shown when present. Per-position P/L still needs a defined producer.

### 5.9 Operator API, health, controls, and alert model

Merged routes:

| Method and path | Access | Capability |
| --- | --- | --- |
| `GET /health` | Public | Liveness only; returns `{"status":"ok"}`. |
| `GET /health/detail` | Operator | Application, broker, and strategy health. |
| `GET /health/strategies` | Operator | Strategy heartbeat collection. |
| `GET /operator/login` | Public | Login form. |
| `POST /operator/login` | Token form | Establish browser session. |
| `GET /operator` | Operator | Server-rendered dashboard. |
| `GET /operator/fragment` | Operator | Refreshable dashboard fragment. |
| `GET /operator/state` | Operator | Complete operator JSON snapshot. |
| `GET /operator/kill-switch` | Operator | Current kill-switch state. |
| `POST /operator/pause` | Operator | Persist `paused`. |
| `POST /operator/emergency-stop` | Operator | Persist `halted`. |
| `POST /operator/rearm` | Administrator | Persist `running` after manual review. |

Programmatic clients use the `x-operator-token` header. Query-string token compatibility exists in merged code, but the UI must never generate token-bearing URLs.

The state model supports:

- provider-neutral broker refresh;
- clearly labelled last-known portfolio data after a failed refresh;
- application and strategy health;
- strategy registration and heartbeat;
- local orders, fills, and signals;
- startup recovery and reconciliation summaries;
- alerts and per-destination delivery status;
- redacted error conditions.

The alert router can fan out to injected phone-push and email sinks. The merged default application does not construct concrete sinks.

### 5.10 Persistence and observability

The database and migrations cover:

- signals;
- risk decisions;
- orders;
- fills;
- system events;
- audit notes;
- candles;
- portfolio snapshot batches;
- positions;
- balances;
- equity;
- discrepancies.

Other observability capabilities:

- structured JSON logging;
- correlation IDs spanning signal, approval, order, and fill;
- secret-field scrubbing;
- provider error normalization;
- token-bucket rate limiting;
- bounded retry behavior;
- circuit-breaker health.

Not all persisted records currently have read APIs suitable for a browser. The design may reserve views for them, but implementation will require bounded query endpoints, filtering, and pagination.

### 5.11 Deployment, backup, restore, and probes

- Local Python startup and Docker Compose deployment.
- VPS Compose override with PostgreSQL and persistent kill-switch state.
- Alembic migrations run before the application.
- Hardened bootstrap and systemd backup timer/service assets.
- Nightly PostgreSQL custom-format dump, `age` encryption, plaintext cleanup, and immutable off-box upload.
- Restore verification into a scratch database with manifest and row-count checks.
- Restart/reboot drill scripts with fail-closed checks.
- Manual/scheduled GitHub restore-drill workflow.
- Drill refusal in live mode or with trade-capable credential scope.
- Owner-run, bounded probes for Gemini Sandbox lifecycle, Coinbase sandbox fixture capture, and Coinbase read-only reconciliation.

These are operational capabilities, not a browser control surface. The UI may display redacted results and timestamps after a proper read model exists; it must not expose hostnames, IP addresses, bucket names, tokens, or provider secrets.

### 5.12 Verification boundary

Capability in code is not the same as completed real-world evidence. The current evidence ledger says:

- 10 of 17 Phase 1 criteria are fully proven by automated tests;
- Coinbase sandbox fixtures were captured and replayed successfully;
- Coinbase read-only reconciliation passed against a real View-only account on 2026-09-27;
- a Gemini Sandbox trading-loop and lifecycle run passed on 2026-09-27 except for the partial-fill condition, which the available book did not produce;
- the shared adapter contract still needs a real-venue run where applicable;
- a real public-stream disconnect plus 72 hours of continuous ingest remain soak evidence;
- seven days of unattended scheduled reconciliation remain soak evidence;
- Coinbase production order placement remains prohibited until Phase 2.

Future readiness or evidence UI must keep four states separate: automated/local evidence, exact-head CI, agent-run infrastructure verification, and owner-run provider verification. Never turn a mocked, fixture-backed, planned, or partial result into a “verified live” badge.

## 6. Current operator-state data map

The next UI should treat `GET /operator/state` as the primary merged read model.

| JSON path | UI treatment |
| --- | --- |
| `application.status` | Global health badge; do not equate with `/health` liveness. |
| `application.heartbeat` | “App heartbeat” timestamp with age. |
| `trading.mode` | Persistent, high-prominence environment badge. Unexpected `live` is critical. |
| `trading.credential_scope` | Security context; never reveal credential contents. |
| `trading.strategy_version` | Version label with `unknown` state. |
| `connectivity.status/detail/checked_at` | Broker card with freshness. |
| `risk.kill_switch` | Primary safety state and control context. |
| `recovery.*` | Startup-recovery card with counts and completion time. |
| `reconciliation.*` | Run totals, last result/time, and discrepancy count. |
| `portfolio.status/detail` | Current versus unavailable/last-known banner. |
| `portfolio.balances[]` | Asset, available, hold, and `as_of`. |
| `portfolio.positions[]` | Symbol, quantity, average price caveat, and `as_of`. |
| `portfolio.pnl.*` | Explicit unavailable state until a trustworthy producer exists. |
| `orders[]` | Expandable order table with request, status, fills, IDs, and times. |
| `fills[]` | Fill table with side, quantity, price, fee, fee asset, and time. |
| `signals[]` | Signal table with strategy version and correlation link. |
| `strategies[]` | Heartbeat status, version, detail, last seen, and staleness. |
| `alerts[]` | Severity timeline with delivery outcomes. |
| `errors[]` | Redacted error timeline. |
| `alert_destinations[]` | Configured/not-configured indicators only. |
| `updated_at` | Page snapshot time and age. |

## 7. Safety-critical interaction rules

These rules are product requirements, not visual preferences:

1. **PAUSE** and **EMERGENCY STOP** must work with JavaScript disabled.
2. **EMERGENCY STOP** must remain visible without scrolling on desktop and mobile.
3. A control request must end on a clear, server-rendered confirmation state.
4. **RE-ARM** is never presented as a recovery shortcut. Gate it by administrator role and require a confirmation step that repeats unresolved warnings.
5. `running` means the kill switch permits work subject to every other gate. It does not mean the system is healthy or actively trading.
6. Never use color as the only carrier of state. Pair color with text, iconography, and where appropriate shape.
7. Never animate green success in a way that implies profitability.
8. “Unavailable,” “unknown,” “stale,” “last known,” and numeric zero require distinct representations.
9. No UI may submit an arbitrary trade. The current API has no general trading-command endpoint.
10. Never display or place tokens, keys, recipient addresses, private topic URLs, provider payloads, or infrastructure identifiers in page content, URLs, analytics, or client logs.
11. A public liveness response must never be presented as full system health.
12. Destructive or safety-reducing actions need clear focus order, keyboard operation, and a response that survives a client-side failure.

## 8. Recommended information architecture

The first implementation can remain one server-rendered route while presenting the following sections. Later iterations may split them into routes after bounded read endpoints exist.

### 8.1 Overview / command center

The default operator view should contain:

- fixed mode/environment and snapshot-age bar;
- kill-switch state plus role-appropriate controls;
- active incident/degraded-state banner;
- startup recovery, broker connectivity, strategy heartbeat, market-data status when available, and reconciliation status;
- portfolio summary with explicit data freshness;
- recent orders/fills/signals;
- recent alerts/errors;
- link or disclosure for operational detail.

Suggested desktop hierarchy:

```text
+--------------------------------------------------------------------------------+
| Algorithmic Crypto Trader | PAPER | snapshot 8s ago | Operator                 |
+--------------------------------------------------------------------------------+
| SAFETY: RUNNING                         [PAUSE] [EMERGENCY STOP]                 |
| Running permits work; all other risk gates still apply.                        |
+-----------------------+-----------------------+--------------------------------+
| Broker                | Strategy              | Reconciliation                 |
| healthy · checked 8s  | unknown / heartbeat   | clean · last run 2m ago        |
+-----------------------+-----------------------+--------------------------------+
| Startup recovery      | Portfolio freshness   | Alerts                         |
| reconciled            | current · broker auth | 0 critical · delivery status   |
+--------------------------------------------------------------------------------+
| Balances and positions                                                          |
+--------------------------------------------------------------------------------+
| Recent activity: signals -> risk -> orders -> fills                             |
+--------------------------------------------------------------------------------+
| Errors, discrepancies, and alert timeline                                      |
+--------------------------------------------------------------------------------+
```

On mobile, preserve the same reading order and pin the emergency action without hiding state behind hover interactions.

### 8.2 Activity

Design an audit-oriented activity view that can eventually connect:

`signal -> risk decision -> order -> fill(s) -> portfolio/reconciliation effect`

Merged data already has identifiers and correlation IDs, but a complete browser view requires read endpoints for risk decisions, audit lineage, events, and bounded historical queries.

### 8.3 Portfolio

Initial merged-data view:

- balances by asset;
- available versus held;
- positions by symbol;
- data source and `as_of` age;
- unmistakable unavailable/last-known treatment.

Do not visualize per-position P/L or use average price as cost basis until a trustworthy P/L producer is defined (issue `#30` made an unknown cost basis explicit).

### 8.4 Risk and safety

Design for:

- kill-switch state and history;
- current mode and credential scope;
- latest risk refusal and failed gate;
- ordered risk-gate status;
- daily-loss/drawdown state;
- cooldown, staleness, and exchange-constraint reasons;
- re-arm checklist and approval context.

Only kill-switch status/control is exposed by the merged HTTP API. Detailed gate views require new read models.

### 8.5 System health and operations

Design for:

- application heartbeat;
- broker status and refresh age;
- market-data connection/freshness when the paper runtime is merged;
- strategy heartbeat and last cycle;
- reconciliation runs and discrepancies;
- startup recovery;
- alert destinations and delivery results;
- backup/restore/drill evidence when a safe read model is added.

## 9. Roadmap additions the design must anticipate

### 9.1 Draft: paper runtime and concrete alerts (`#51`, PR `#54`)

This open draft currently adds:

- paper-only runtime startup from the FastAPI lifespan;
- public Coinbase ticker and five-minute candle ingestion;
- closed-candle persistence and gap fill;
- moving-average strategy cycles through the existing risk/execution path;
- shared locking with reconciliation;
- fail-closed market data, history, quote, persistence, and configuration behavior;
- runtime status, detail, last-cycle status, and last-cycle time in operator state;
- primary strategy heartbeats;
- configurable ntfy phone push and SMTP email sinks;
- delivery-failure recording with redacted errors.

Design reservation:

- market-data/runtime card;
- last cycle result and timestamp;
- last closed candle / quote age;
- strategy activity state that is distinct from app liveness;
- configured alert destinations and per-delivery results.

Do not mark these capabilities as merged until PR `#54` lands.

### 9.2 Backlog: explicit unknown cost basis (`#30`)

This change will replace the current zero placeholder with `None` through adapters, storage, reconciliation, and the ledger.

Design reservation:

- show “Cost basis unavailable” rather than `$0.00`;
- keep market value separate from cost basis;
- delay per-position P/L until both data and calculation contracts are trustworthy.

### 9.3 Backlog: 30-day unattended paper soak (`#12`)

The soak calls for daily review of:

- equity;
- day P/L;
- trades;
- rejections by risk gate;
- reconciliation status;
- error counts;
- uptime;
- restart, disconnect, divergence, kill-switch, backup, and restore evidence;
- incident history.

Feasible UI additions for this phase:

- daily operations digest;
- 30-day criterion tracker;
- uptime and freshness trends;
- equity and day-P/L chart with explicit data source;
- risk-rejection histogram;
- reconciliation/discrepancy trend;
- incident log with resolved/unresolved state;
- restart and restore-drill evidence cards.

Most of these need new persisted read models or aggregation endpoints. They should not be faked from browser memory.

### 9.4 Backlog: controlled Coinbase live activation (`#13`)

The future live phase requires the smallest practical exposure, tight loss limits, paper/live comparison, review of every fill for two weeks, measured fees/slippage, and a verified production emergency stop.

Design reservation:

- unmissable production/live environment treatment;
- exposure and risk-limit summary;
- paper-versus-live decision comparison;
- fill-review queue;
- realized fee/slippage comparison;
- explicit emergency-stop verification record.

The UI must not offer live activation or provider credential changes merely because these concepts are designed. Phase 1.5 and owner authorization remain hard prerequisites.

### 9.5 Backlog: post-Coinbase venue decision (`#14`)

A future adapter must preserve the same strategy, risk, portfolio, and reconciliation contracts while safely degrading capabilities.

Design reservation:

- provider-neutral labels;
- broker/environment badge driven by capability data, not hard-coded Coinbase copy;
- capability matrix for streaming, historical candles, native edit, preview, order types, increments, and staleness;
- account/portfolio separation if more than one venue is ever active;
- clear “polling fallback” and “cancel-and-replace” states.

No additional venue has been selected.

## 10. Proposed UI iterations that are feasible but not yet carded

These additions fit the current architecture but are not implementation commitments. Create and claim cards before building them.

### Iteration A: truthful command-center redesign

- Style the existing Jinja pages using local CSS and the manual's token system.
- Load HTMX locally or remove the inactive attributes; do not depend on a third-party CDN for emergency controls.
- Render JSON-only merged fields: freshness, reconciliation, credential scope, alert delivery, and update time.
- Add responsive tables/cards, visible focus states, skip link, and reduced-motion handling.
- Add sign-out by implementing a server endpoint that clears the session cookie.
- Replace token query compatibility in browser workflows with form/cookie or header-only use.
- Add accessible confirmation flows for pause, halt, and re-arm while retaining no-JavaScript POST behavior.

### Iteration B: operational activity and diagnostics

- Add bounded, paginated read endpoints for persisted risk decisions, system events, discrepancies, audit lineage, and historical orders/fills.
- Add search/filter by symbol, status, time, strategy version, failed risk gate, and correlation ID.
- Add an expandable event timeline that explains why an order did or did not happen.
- Add copy-safe identifiers with no secret-bearing provider payloads.
- Add server-side pagination; do not ship an unbounded audit history to the browser.

### Iteration C: research and replay workspace

- Upload/select approved historical datasets without provider writes.
- Configure backtest cost assumptions and walk-forward windows.
- Present mandatory report fields, equity/drawdown, exposure, trade distribution, and regime/year breakdowns.
- Compare versioned strategy runs and export a redacted report.
- Make clear that backtest results are not profitability promises.

### Iteration D: soak and readiness console

- Daily digest and criterion tracker from issue `#12`.
- Incident and drill evidence timeline.
- Explicit readiness gates for Phase 2, all defaulting to incomplete until evidence exists.
- Owner-run verification labels that remain separate from local tests and CI.

## 11. Visual and content direction

### 11.1 Visual tone

- Calm operations console, not a consumer exchange.
- Dense enough for rapid scanning, with progressive detail rather than a wall of cards.
- Neutral surfaces; reserve saturated color for operational meaning.
- Use tabular numerals and monospace treatment for timestamps, IDs, quantities, and prices.
- Preserve the manual's warm-neutral identity where it does not reduce status clarity.

### 11.2 Status vocabulary

Use the system's exact terms where they are contractual:

- `running`, `paused`, `halted`;
- `backtest`, `replay`, `paper`, `live`;
- `pending_submit`, `unknown`, `open`, `partially_filled`, `filled`, `canceled`, `rejected`.

Prefer plain-language supporting copy:

- **Current** — refreshed successfully from the authoritative source.
- **Last known** — retained for diagnosis; not proof of current state.
- **Unavailable** — the source could not be read.
- **Unknown** — the system cannot determine the value or outcome yet.
- **Stale** — known data is older than its allowed freshness window.
- **Not configured** — the capability is intentionally absent.
- **Not started** — a producer has not begun running.

### 11.3 Suggested semantic colors

Reuse or adapt the manual tokens:

- neutral/background: `#eef0ec`, `#f8f9f6`, `#18201d`;
- informational/accent: muted gold `#7d6224`;
- healthy/current: green `#2f6b4a`;
- warning/paused/stale: amber `#8a5f14`;
- critical/halted/unavailable divergence: red `#96382f`.

Check WCAG contrast in both light and dark modes. Do not assign green to profit and red to loss if those colors already carry system-health meaning on the same screen.

### 11.4 Content rules

- Always show source and freshness near balances, positions, market data, and health.
- Pair an error with the safe next action, not a generic “Something went wrong.”
- Avoid promising that `running` means orders will occur.
- Avoid “connected” without a check time.
- Avoid “P/L: 0” when P/L is unavailable.
- Avoid “average price: 0” when cost basis is unknown.
- Keep provider names out of shared component labels unless the provider identity itself matters.

## 12. Accessibility and responsive requirements

- Meet WCAG 2.2 AA color contrast and interaction expectations.
- Use a logical heading hierarchy and landmark structure.
- Provide a skip link and visible keyboard focus.
- Preserve DOM reading order when cards reflow.
- Give every form control an explicit label and helpful error association.
- Use `aria-live` sparingly for refreshed status; do not announce the entire dashboard every 15 seconds.
- Preserve focus across partial refreshes.
- Do not place critical meaning only in tooltips or hover states.
- Respect `prefers-reduced-motion` and `prefers-color-scheme`.
- Keep emergency stop reachable and legible at 320 CSS pixels wide.
- Tables need small-screen alternatives or controlled horizontal scrolling with retained headers/context.
- Use local-time display only when the underlying UTC timestamp remains available.

## 13. Implementation boundaries for the UI agent

The next agent should:

- read `AGENTS.md` and claim the relevant card before editing;
- work in a dedicated worktree;
- keep pages provider-neutral;
- reuse the server-rendered Jinja/FastAPI path unless a card explicitly changes the frontend architecture;
- preserve HTML fallbacks for safety controls;
- add focused tests for authorization, degraded data, staleness, keyboard/semantic behavior, and no-JavaScript controls;
- verify that no token or secret reaches rendered HTML, URLs, fixtures, logs, or screenshots;
- report local validation, exact-head CI, and owner-run evidence separately.

The next agent should not:

- create an arbitrary order-entry ticket;
- enable or activate live mode;
- weaken administrator re-arm authorization;
- turn unavailable or last-known data into a healthy state;
- calculate cost-basis P/L from the current zero placeholder;
- expose raw provider payloads or deployment secrets;
- make the emergency controls depend on JavaScript;
- claim draft PR `#54` behavior as merged before it lands;
- implement unbounded history endpoints solely to populate a design.

## 14. Suggested delivery sequence

### Delivery 1: merged-data dashboard

Design and implement a responsive, accessible command center from existing `/operator/state` data. Include safety controls, mode, freshness, recovery, reconciliation, broker/portfolio state, activity summaries, alert/error delivery, and clear unavailable states.

### Delivery 2: runtime-aware dashboard

After PR `#54` merges, add paper runtime, market-data, last-cycle, and concrete alert-sink status without changing the core hierarchy.

### Delivery 3: bounded operational history

Add explicit read models and routes for risk decisions, discrepancies, events, and audit lineage, then build filtered activity and diagnostics views.

### Delivery 4: soak/readiness views

Add issue `#12` daily digest, incidents, evidence, trends, and readiness criteria. Keep every criterion evidence-backed.

### Delivery 5: controlled-live support

Only after Phase 1.5 acceptance and a separately authorized card, add the paper/live comparison and live-fill review surfaces required by issue `#13`.

## 15. Design acceptance checklist

A completed design should demonstrate at least these states:

- logged out;
- authenticated operator;
- authenticated administrator;
- backtest with no broker configured;
- paper mode with healthy broker;
- broker unavailable with last-known data;
- startup recovery halted;
- clean and divergent reconciliation;
- `running`, `paused`, and `halted` kill-switch states;
- unknown/stale strategy heartbeat;
- no orders, many orders, partial fill, unknown order, and rejected order;
- no alerts, critical alert, and failed alert delivery;
- P/L unavailable;
- cost basis unknown;
- narrow mobile viewport;
- keyboard-only interaction;
- JavaScript disabled for pause, emergency stop, and re-arm;
- future paper runtime not started, healthy, degraded, and failed.

## 16. Source map

Use these files as the primary implementation sources:

- `api/templates/operator_login.html` — merged login markup.
- `api/templates/operator.html` — merged page shell.
- `api/templates/operator_fragment.html` — merged dashboard content.
- `api/routes.py` — authentication, sessions, JSON routes, and controls.
- `api/operator.py` — operator-state read model and degraded refresh semantics.
- `api/alerts.py` — alert model and merged injected-sink router.
- `docs/user-manual.css` — existing visual tokens and responsive documentation patterns.
- `docs/user-manual.md` — operator language, status meaning, and safety procedures.
- `core/models.py` — exact modes, order states, kill-switch states, and field shapes.
- `risk/engine.py` and `risk/kill_switch.py` — ordered gates and safety-state behavior.
- `app/main.py`, `app/recovery.py`, and `portfolio/scheduler.py` — runtime composition, startup recovery, and reconciliation.
- `db/models.py` — persisted records that may support future read views.
- `docs/crypto-algo-trading-planning-document.md` — accepted phase roadmap.
- `docs/phase-1-gate-acceptance.md` — verified and still-open evidence.

Live planning records checked for this report:

- [Issue #30: unknown cost basis](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/30)
- [Issue #51: paper runtime and alerts](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/51)
- [PR #54: draft paper runtime implementation](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/pull/54)
- [Issue #12: 30-day paper soak](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/12)
- [Issue #13: controlled Coinbase live activation](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/13)
- [Issue #14: future broker decision](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/14)

## 17. Final design principle

The UI succeeds when an operator can tell, in seconds, whether the system is safe to leave alone, needs investigation, or must be halted—and when the screen never claims more certainty than the code and evidence can support.
