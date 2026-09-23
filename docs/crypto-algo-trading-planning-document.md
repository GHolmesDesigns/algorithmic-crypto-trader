# Personal Algorithmic Crypto Trading Platform

## Implementation planning document

**Document revision:** Rev. B — 21 September 2026
**Supersedes:** Rev. A, 20 September 2026
**Planning basis:** Feasibility study **Rev. B** (Coinbase live / Gemini sandbox / Kraken second)
**Primary decision:** Phase 1 uses Coinbase Advanced Trade for the production/live-trading integration and Gemini Sandbox for rehearsal. No unofficial integration is in scope.

### Phase numbering crosswalk

This document re-scopes the feasibility study's eleven phases into four. Use this mapping whenever the two documents are discussed together.

| This document | Feasibility study | Content |
|---|---|---|
| Phase 0 | Phase 0 | Read-only API validation |
| Phase 1 | Phases 1–8 | Foundation through operator surface and deployment |
| Phase 1.5 | (unnamed in the study; implied by the Phase 8 exit criterion) | 30-day unattended paper soak |
| Phase 2 | Phase 9 | Tiny-value live activation on Coinbase |
| Phase 3 | Phase 10 | Multi-broker expansion |

### Changes in Rev. B

Applied from the review recorded in `crypto-algo-trading-planning-document-REVIEW.md`: work packages reordered so no order-capable adapter precedes the risk engine; Phase 1.5 named and added to the calendar; four acceptance criteria added; structured logging moved into the foundation package; per-package exit criteria added; effort estimate widened; stale Coinbase links corrected; mode vocabulary reconciled; two risks added.

## 1. Outcome and operating principle

Build a personal, single-operator crypto-trading system whose strategy code is isolated from exchange-specific behavior, whose orders are gated by an independent risk engine, and whose local state can be reconciled against the broker at any time.

The system must be able to run the same strategy through four execution modes. These are the literal values of `TRADING_MODE`; the plan and the code use the same words throughout.

1. **`backtest`** — historical Coinbase candles with explicit fees, spread, slippage, and walk-forward validation.
2. **`replay`** — recorded live streams replayed at speed through the real strategy, risk, and execution code against `SimulatedBroker`.
3. **`paper`** — live Coinbase market data against `SimulatedBroker`, or against `GeminiBroker(sandbox)` for matching-engine behavior. Continuous, unattended.
4. **`live`** — Coinbase Advanced Trade only, using a production key limited to View + Trade and with transfers/withdrawals disabled.

"Rehearsal," where it appears below, is shorthand for `replay` plus `paper`; it is not a mode value.

The guiding rule is: strategy sophistication is negotiable; safety controls, auditability, reconciliation, and emergency stop behavior are not.

## 2. Settled venue roles

| Venue | Phase 1 role | Allowed environment | Purpose |
|---|---|---|---|
| Coinbase Advanced Trade | Production adapter and eventual live venue | Sandbox, read-only production, then tightly limited production | Market data, historical candles, account state, order lifecycle, fills, reconciliation, and real trades after all gates pass |
| Gemini | Rehearsal adapter | **Sandbox only** | Real matching-engine behavior, partial fills, rejects, cancellations, and order-state testing with no capital at risk |
| Internal `SimulatedBroker` | Paper/replay adapter | Local or VPS | Pessimistic paper trading against live Coinbase data, deterministic replay, and fault injection |

Gemini is not a substitute for live-market paper trading: its market is synthetic. The internal simulator is therefore required as well. Coinbase’s sandbox is used for wire-format and error-path testing; it is not treated as a realistic exchange simulator.

The following are explicitly deferred until after Phase 1 is stable: Kraken, Robinhood, Webull, Binance.US, cross-venue routing, leverage, shorting, market making, order-book strategies, ML strategies, multi-user access, and tax-lot accounting.

## 3. Phase 0 — validation gate before implementation

Phase 0 is a short, read-only validation pass. It converts assumptions from the research document into facts tied to the intended accounts and jurisdiction.

### Required work

- Create a Coinbase API key with View-only permissions and verify Ed25519/JWT authentication.
- Capture the response shapes for products, BTC-USD and ETH-USD candles, and accounts.
- Capture Coinbase public WebSocket ticker/candle messages for replay fixtures.
- Confirm current Coinbase fee tier, rate limits, product increments, minimum order sizes, and sandbox behavior.
- Create or access a Gemini Sandbox account and verify test-balance provisioning, authentication, order placement, matching, partial fills, cancellation, and rejection behavior.
- Confirm the applicable developer agreements, automated-trading restrictions, and geographic eligibility.
- Record all findings in a versioned capability sheet; no implementation assumption may remain undocumented.

### Exit criteria

Phase 0 is complete when the captured Coinbase and Gemini payloads parse into draft models, the exchange capabilities are recorded, and a non-production order lifecycle can be demonstrated in Gemini Sandbox without using a production trading key.

## 4. Phase 1 — dual-venue MVP

**Objective:** deliver a production-capable Coinbase integration, a permanent Gemini Sandbox rehearsal integration, and an internal live-market simulator behind one honest broker abstraction.

**Indicative effort:** 12–16 weeks of build, depending on data-history depth and operational polish, **plus a further ~6 weeks of Phase 1.5 soak before Phase 2 can open.** The estimate is an engineering planning range, not a commitment.

The source study's own per-phase figures for this scope sum to roughly 15 weeks before the Gemini adapter that this plan adds. An earlier 8–12 week figure assumed single-operator compression that has not been demonstrated. If the schedule slips, **the response is to move the date, not to reduce the safety scope** — the risk gates, reconciliation, and soak period are the deliverable.

### Phase 1 work packages

Packages are ordered by dependency and each carries its own exit criterion. A package is not complete until its exit criterion has been demonstrated, not merely coded.

**Ordering rule:** no adapter capable of submitting a real order is built before the risk engine exists. A convention not to call `place_order()` is not a control.

#### 1. Foundation, domain model, and observability

- Python 3.12+ service with `asyncio` for streaming and execution.
- Repository boundaries: `core/`, `brokers/`, `data/`, `strategy/`, `risk/`, `execution/`, `portfolio/`, `api/`, and `tests/`.
- Frozen Pydantic models for candles, quotes, balances, positions, signals, order requests, orders, fills, market state, and risk approvals.
- `Decimal` for all prices, quantities, fees, balances, and P/L; prohibit floating-point money arithmetic.
- `BrokerInterface` plus an explicit `BrokerCapabilities` object. Capabilities must describe streaming, historical candles, native order edit, preview, order types, increments, and staleness expectations.
- `TRADING_MODE=backtest|replay|paper|live`, with a loud startup banner and a second explicit confirmation required for `live`.
- **Boot-time credential-scope assertion:** refuse to start if a trade-capable key is present while `TRADING_MODE` is `backtest`, `replay`, or `paper`, or if only a View-only key is present while `TRADING_MODE` is `live`. Mode and credential scope must agree before anything else initializes.
- **Structured JSON logging with correlation IDs spanning signal → approval → order → fill**, plus the secret-scrubbing processor and its unit test. These are foundation work, not operator-surface work: packages 2–5 cannot be debugged without them, and retrofitting correlation IDs means touching every component twice.
- **Rate limiting active in every mode.** The token-bucket limiter and circuit breaker are on in `backtest` and `replay` as well as `paper` and `live`, so a runaway development loop cannot burn a production key's quota.
- PostgreSQL with SQLAlchemy and Alembic. SQLite is limited to isolated tests and portable backtest archives.
- Docker Compose for the application and database; CI runs formatting, linting, type checks, and tests.

**Exit:** a clean checkout builds and runs with one command; the service cannot start in `live` mode by accident or with a mismatched credential scope; the scrubber test passes against a fixture containing every known secret-bearing field name.

#### 2. Coinbase market-data service

- Historical candle backfill with resumability and idempotent writes.
- Public WebSocket ingestion with heartbeat monitoring, reconnect backoff, and REST gap fill after every disconnect.
- Canonical normalization into `Candle` and `Quote` objects with `source`, `as_of`, and ingestion timestamps.
- Validation for duplicate bars, gaps, timestamp order, OHLC consistency, non-negative volume, and suspicious outliers.
- A quote cache exposing quote age and a raw-stream recorder for replay.

**Exit:** 72 hours of continuous ingest with zero unexplained gaps, and a verified REST gap fill following at least one real disconnect.

#### 3. `SimulatedBroker` and the shared broker contract suite

The contract suite is authored here, against `SimulatedBroker`, and is the specification every later adapter must satisfy on arrival. Building it before any real adapter is what keeps the abstraction honest.

- Fill market orders at the far side of the spread plus configured slippage.
- Fill limits only when the simulated conditions permit; generate deliberate partial fills on a schedule, so the partial-fill path is exercised in normal operation rather than discovered in production.
- Inject rejects, timeouts, duplicate acknowledgements, out-of-order fills, 429 responses, and total unavailability.
- Run both accelerated replay and continuous paper mode against live Coinbase data.

**Exit:** the contract suite passes against `SimulatedBroker`, and every injected fault is exercised in CI.

#### 4. Research and strategy layer

- Event-driven backtester using closed Coinbase bars only; no look-ahead.
- Configurable maker/taker fees, spread, slippage, fee currency, and partial-fill assumptions.
- Walk-forward train/test windows and a sealed final holdout.
- Mandatory reports: data source/window, symbols, granularity, fee and slippage assumptions, trade count, exposure, return distribution, drawdown and duration, per-year/per-regime results, and strategy version hash.
- Start with one deliberately simple reference strategy, such as a moving-average crossover or threshold rebalance. The first strategy is a machine-validation instrument, not a profitability claim.
- Strategy code may receive market state and return a signal; it may not import the broker, database, HTTP client, or network code.

**Exit:** the known-answer tests pass — an always-buy strategy reproduces buy-and-hold minus fees to the cent, and a look-ahead canary strategy produces absurd returns, proving the guard fails when removed. Walk-forward runs end to end and every mandatory report field is present.

#### 5. Risk engine, execution engine, portfolio, and reconciliation

The execution engine accepts only a `RiskApproval`, never a raw strategy signal.

Implement and individually test these gates in order:

1. Kill switch.
2. Operator pause and trading window.
3. Broker/API health.
4. Stale-price detection.
5. Abnormal volatility.
6. Price sanity and independent-reference divergence.
7. Duplicate-order prevention.
8. Symbol cooldown.
9. Maximum open positions.
10. Maximum trade notional.
11. Maximum per-symbol position.
12. Maximum aggregate crypto allocation.
13. Minimum cash reserve.
14. Maximum daily loss.
15. Maximum drawdown.
16. Slippage limit.
17. Exchange constraints from current product metadata.

The kill switch must have three persistent states—`RUNNING`, `PAUSED`, and `HALTED`—and three independent actuation paths: dashboard, authenticated API, and a file/environment flag read each loop. Automatic trips require manual re-arm and an audit event. PAUSE and STOP must work with JavaScript disabled and must survive restart.

The reconciler must compare local and Coinbase state for orders, fills, positions, and balances. The broker is authoritative. A divergence must create a discrepancy event, alert the operator, and trip the configured safety response before further entries.

**Exit:** 100% branch coverage on the risk and execution packages; every gate demonstrably blocks a paper order; the fail-closed suite passes with each risk input nulled in turn; and the kill switch is verified through all three actuation paths and across a restart.

#### 6. Coinbase and Gemini adapters

Built only after package 5 is complete. Both are implemented against the unchanged contract suite from package 3.

**`CoinbaseBroker`**

- Use the official Coinbase Advanced Trade client where it fits; isolate SDK-specific types inside the adapter.
- Support balances, positions, products, quotes, candles, order submission, cancellation, native edit where supported, status, fills, and authenticated user-order events.
- Native order edit is **limit orders only, on open orders, and cannot reduce below the filled size.** Everything outside those bounds is cancel-and-replace, and `BrokerCapabilities` must say so rather than leaving callers to discover it.
- Handle JWT expiry transparently.
- Persist a deterministic `client_order_id` before submission.
- Treat timeout or ambiguous submission as `UNKNOWN`; resolve by querying the venue before any retry.
- Add a token-bucket limiter, bounded backoff for 429 responses, and a circuit breaker that feeds the risk engine.
- Support authenticated WebSocket events with polling fallback.

**`GeminiBroker`**

- **Hardcode the sandbox hosts inside the adapter.** The base URL is not a configuration value, and the adapter raises at construction if any endpoint does not resolve to a `*.sandbox.gemini.com` host. A prose instruction not to point this at production is not a control.
- Keep sandbox credentials and configuration separate from Coinbase production credentials.
- Exercise the full order lifecycle: submit, acknowledge, working, partial fill, fill, cancel, reject, timeout, and recovery.

**Exit:** the contract suite passes unmodified against `SimulatedBroker`, `GeminiBroker(sandbox)`, and `CoinbaseBroker`; the full Gemini order lifecycle has been exercised end to end; and Coinbase read-only production calls reconcile cleanly against the account.

#### 7. Operator surface

- FastAPI with server-rendered Jinja/HTMX pages for portfolio, positions, P/L, orders, fills, signals, strategy version, connectivity, risk state, and errors.
- Authenticated `PAUSE` and `EMERGENCY STOP` POST endpoints with an HTML fallback.
- Health endpoints and heartbeats for the application and each strategy; alert routing to phone push and email.

**Exit:** the kill switch fires with JavaScript disabled; the dashboard renders in a degraded but correct state when the broker is unreachable, rather than blank; every alert condition has been induced and verified end to end.

#### 8. Deployment, backups, and restore

- Start locally through Phase 1 development; deploy to a small VPS only after the service is restart-safe.
- Docker Compose deployment; static IP for exchange-key IP allowlisting where the venue supports it.
- Nightly encrypted off-box PostgreSQL backups.

**Exit:** the service survives restart on the VPS with correct state recovery, and **a backup has been restored into a scratch database and verified** — configured is not the same as tested. This must be true before Phase 1.5 begins, not before live activation.

## 5. Phase 1 acceptance criteria

Phase 1 is complete only when all of the following are true:

- The same strategy runs through backtest, replay, `SimulatedBroker`, Gemini Sandbox, and Coinbase without strategy-code changes.
- The shared broker contract suite passes for `SimulatedBroker`, `GeminiBroker(sandbox)`, and `CoinbaseBroker`.
- Gemini Sandbox has exercised submit, partial fill, cancel, reject, timeout, and recovery paths.
- Coinbase read-only production calls reconcile cleanly against the account.
- Coinbase sandbox fixtures cover authentication, pagination, error payloads, and the documented error scenarios.
- Every order is linked to a signal, strategy version, risk decision, and fill record.
- No ambiguous timeout can create a duplicate order.
- Every missing or stale risk input fails closed.
- Kill switch behavior is verified through all three actuation paths and across a restart.
- A forced WebSocket disconnect is recovered with gap fill and no unexplained data loss.
- A process restart recovers order and portfolio state correctly.
- The process killed with `SIGKILL` between the pre-submit persist and the API call recovers correctly: on restart the `PENDING_SUBMIT` row is found and resolved by querying the venue for that `client_order_id`, and the resulting position is single, not double. This is the most important idempotency test in the system.
- With the database unavailable, no order is submitted while the pre-submit record cannot be persisted; the system halts rather than trading un-audited, and recovers cleanly when the database returns.
- The scheduled reconciler runs unattended for at least seven days. A deliberately injected divergence in positions, and separately in balances, each produces a discrepancy event, an operator alert, and the configured safety trip — and the broker's record is the one that survives correction.
- `GeminiBroker` raises at construction against any non-sandbox host, and the host is not settable from configuration.
- Backtest/replay parity is demonstrated on at least three windows, including one high-volatility window.
- The application cannot enter live mode without the explicit confirmation flag and a trade-capable Coinbase key.

## 6. Phase 1.5 — unattended soak

Phase 1 proves the system is correct. Phase 1.5 proves it is *durable*, which is a different claim and cannot be made by a test suite. This period was previously implied by the Phase 2 preconditions but belonged to no phase, had no deliverable, and was absent from the schedule; it is named here so it cannot be silently compressed.

**Entry:** Phase 1 accepted in full; deployed to the VPS; backup restore verified.

**Duration:** 30 consecutive days of unattended `paper` operation. The two-week VPS stability window runs concurrently, not afterwards.

### Required activities

- Daily review of the digest: equity, day P/L, trades, rejections by gate, reconciliation status, error counts, uptime.
- One deliberate restart drill per week, with state recovery verified against the broker afterwards.
- At least one full backup restore into a scratch database during the period.
- Log every incident, including ones that resolved themselves. An unexplained self-resolving anomaly is a finding, not a non-event.

### Exit criteria

- 30 consecutive days with no unexplained position or balance mismatch.
- At least one WebSocket disconnect survived with correct gap fill.
- At least one process restart survived with correct state recovery.
- At least one reconciliation divergence either observed and explained, or deliberately injected and correctly handled.
- The kill switch fired and verified during the period, not only in testing.
- No open incident without a documented cause.

A failure during soak resets the clock on the affected criterion. The period is not a formality; it is the only evidence available that the system behaves over time rather than in a test harness.

## 7. Phase 2 — controlled Coinbase live activation

Phase 2 is the first phase in which the production Coinbase order path may be enabled. Phase 1 builds and verifies the capability; Phase 2 proves it with tightly bounded exposure.

### Preconditions

- Phase 1.5 soak completed against all of its exit criteria.
- Production Coinbase key limited to View + Trade, with no transfers or withdrawals and IP allowlisting where available.
- Reconciler, alerts, kill switch, and incident runbook approved and tested.

### Activation plan

- Begin at the smallest practical order size and tight daily-loss/drawdown limits.
- Run paper and live decisions in parallel and compare them daily.
- Review every live fill against Coinbase records for the first two weeks.
- Measure realized fees and slippage against the model; update the model only through a versioned change.
- Deliberately fire and verify the production kill switch.

### Exit criteria

Two consecutive weeks of Coinbase live operation with zero reconciliation breaks, explained paper/live differences, slippage within the approved bound, and a verified production emergency stop.

## 8. Phase 3 — expansion only after Coinbase is stable

The next adapter should be selected by a separate decision. The research document favors Kraken ahead of Robinhood or Webull on four grounds: availability in every US state except New York and Maine; native OHLCV and L1/L2/L3 order-book depth; a genuinely different rate-limit model (a tier-based decaying counter rather than a fixed rate) that stresses the abstraction usefully; and mature ecosystem support — Kraken is on Freqtrade's officially supported list and in CCXT, which makes independent cross-checking of results possible.

Two costs come with it and must be carried into that decision rather than discovered during it: **Kraken has no spot sandbox** (its demo environment is derivatives only), and **there is no official Kraken Python SDK** — the usable clients are community-maintained and must be pinned deliberately. None of this is a Phase 1 commitment.

Any future adapter must pass the unchanged contract suite and demonstrate capability degradation safely—for example, polling when streaming is unavailable and cancel-and-replace when native order edit is unavailable. Strategy, risk, portfolio, and reconciliation code must not be rewritten for a new venue.

## 9. Initial data model

The minimum audit spine is:

- `candles`
- `strategies`
- `strategy_versions`
- `signals`
- `orders`
- `fills`
- `positions_snapshot`
- `balances_snapshot`
- `risk_events`
- `system_events`
- `equity_curve`

Orders require a unique `client_order_id`; fills require a unique broker fill identifier. Raw broker payloads may be retained in JSON form only after headers, signatures, JWTs, and secrets are scrubbed.

## 10. Phase 1 risks and mitigations

| Risk | Mitigation |
|---|---|
| Coinbase sandbox is not a real exchange | Use Gemini Sandbox for matching-engine rehearsal and `SimulatedBroker` for live-market paper behavior |
| Ambiguous network timeout causes duplicate trade | Persist idempotency key before submission; resolve `UNKNOWN` through broker query; enforce DB uniqueness |
| Strategy overfits | Use walk-forward testing, sealed holdout, realistic costs, and a simple reference strategy |
| Stale or bad market data produces an order | Track `as_of`, validate ingestion, cross-check prices, and fail closed on staleness |
| Broker/local state diverges | Scheduled reconciliation with broker-authoritative correction and automatic safety trip |
| Credential compromise | View + Trade only, no transfers, separate environment keys, IP allowlisting, secret scrubbing, rotation plan |
| Operational outage | Heartbeats, alerts, restart recovery, backups, and a kill switch independent of the frontend |
| Runaway request loop during development burns a production key's quota or triggers throttling | Token-bucket limiter and circuit breaker active in every mode, including `backtest` and `replay`, not only `live` |
| Mode and credential scope drift — a trade-capable key present in `paper`, or a View-only key in `live` | Boot-time assertion that key permissions match `TRADING_MODE`; refuse to start on mismatch |
| Synthetic Gemini behavior is mistaken for profitability evidence | Use Gemini only for execution correctness; use Coinbase-data simulation and backtests for market-behavior evidence |

## 11. Deferred decisions and open validation items

Before live activation, confirm and record:

- Current Coinbase fees, rate limits, WebSocket limits, and product constraints.
- Gemini Sandbox rate limits, payload semantics, and whether an official Python client exists.
- Gemini Sandbox fidelity — observed spread width, fill latency, and rejection frequency against production behavior. Record the divergence; the sandbox is a correctness test, never a performance estimate.
- State/account eligibility and current API agreement terms.
- Exact alerting destination and incident response ownership.
- Backup retention and restore frequency.
- The single strategy and its initial risk configuration.

No return or profitability assumption is part of this plan. A positive backtest is evidence that a strategy has not yet been disproven, not evidence that it will make money.

## Source and reference links

This plan is derived from the feasibility study, **Rev. B**. The study's Section 16 holds the full reference set — roughly thirty sources covering Coinbase, Robinhood, Webull, Kraken, Gemini, Binance.US, and the third-party frameworks. The subset below is the references Phase 0 and Phase 1 need.

**Coinbase Advanced Trade**

- [Advanced Trade APIs — Overview](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/overview)
- [Advanced Trade Sandbox](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox) — static fixtures, accounts and orders only, no market data
- [Advanced Trade WebSockets — setup, authentication, subscriptions](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/websocket)
- [Edit Order reference](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/edit-order) — limit orders only; confirms the constraint recorded in work package 6
- [Coinbase Advanced Python SDK](https://github.com/coinbase/coinbase-advanced-py)

**Gemini**

- [Gemini Sandbox](https://developer.gemini.com/get-started/sandbox)
- [Gemini Developer Platform](https://developer.gemini.com/)

These links and all exchange capabilities must be re-verified before implementation because API behavior, fees, limits, eligibility, and terms can change.
