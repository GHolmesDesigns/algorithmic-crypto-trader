# Phase 1 gate: integrated acceptance and failure-path evidence

**Issue:** [#11](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/11)
**Evidence date:** 2026-09-24
**Status:** automated evidence complete; owner-run provider checks listed below remain open.

This ledger maps each Phase 1 acceptance criterion in
[the planning document](crypto-algo-trading-planning-document.md#5-phase-1-acceptance-criteria)
to dated evidence or to an explicitly recorded owner-run gap. No live trading is
enabled by this card, and no test contacts an exchange.

## Summary

- **Proven by automated tests, nothing left to run (10 of 17):** criteria 6–9,
  11–13, and 15–17. These cover the audit trail, no duplicate orders after a
  timeout, fail-closed risk inputs, the kill switch, restart and crash recovery,
  database outages, the Gemini host lock, backtest/replay parity, and the
  live-mode guard.
- **Proven at the wire level, but a real provider run is still owed (5 of 17):**
  - the strategy through Gemini Sandbox and Coinbase (1);
  - the adapter contract suite against the real venues (2);
  - the Gemini lifecycle through the adapter (3);
  - Coinbase read-only reconciliation (4);
  - sandbox-captured Coinbase fixtures (5).
- **Proven at the logic level, but needing duration (2 of 17):** the 72-hour
  live market-data run with a real disconnect (10), and the seven-day unattended
  reconciler (14). Both belong to the soak.
- **Defects found and fixed:** building this evidence exposed defects that would
  have failed in production, mostly in the provider adapters. They are listed
  under [defects found by the gate](#defects-found-by-the-gate).

## New components this gate needed

The criteria could not be demonstrated without four pieces that did not exist:

| Component | Purpose |
| --- | --- |
| `app/trading.py` — `TradingCycle` | One loop for every mode. It reads the kill-switch flag file and environment each loop, resolves pending or unknown orders by `client_order_id` before any new entry, records the signal, evaluates the ordered risk gates, records the decision, and submits only an approved `RiskApproval`. A failed audit or order write halts trading. |
| `app/replay.py` — `ReplayRunner` | Replays closed bars through that same loop against `SimulatedBroker`, timed like the backtester: the signal forms on bar *n* and fills at bar *n+1*'s open. |
| `execution/audit.py` and migration `0005_risk_decisions` | Persists every signal and every risk decision, approved or refused. Reports any order missing its signal, strategy version, approved decision, or fills. |
| `portfolio/scheduler.py` and `portfolio/ledger.py` | The unattended reconciler. Each run compares the last broker-authoritative state plus locally recorded fills with the broker, then adopts the broker's record. The service starts it when a broker is configured (`RECONCILE_INTERVAL_SECONDS`, default 300) and reports it under `reconciliation` in the operator state. |

## Evidence ledger

Test files are under `tests/`. "Automated" evidence runs in every CI run of the
Repository preflight workflow.

| # | Acceptance criterion | Evidence (2026-09-24 unless noted) | Remaining owner-run verification |
| --- | --- | --- | --- |
| 1 | The same strategy runs through backtest, replay, `SimulatedBroker`, Gemini Sandbox, and Coinbase without strategy-code changes. | **Automated.** `test_phase1_gate_modes.py::test_one_strategy_runs_unchanged_in_every_mode` runs one unmodified `MovingAverageCrossStrategy` in all five paths. Signals, deterministic client order IDs, and final positions match across modes. Gemini and Coinbase use stateful fakes of their documented REST formats. | Run the same loop against the real Gemini Sandbox. Coinbase order placement is not permitted before Phase 2; its leg stays at the fixture level until then. |
| 2 | The shared broker contract suite passes for `SimulatedBroker`, `GeminiBroker(sandbox)`, and `CoinbaseBroker`. | **Automated.** `test_broker_contract.py` for the simulator; `test_broker_adapters.py` runs the unchanged contract against both adapters on documented response shapes, corrected in this card. | The contract against the real Gemini Sandbox; Coinbase read-only calls (see 4). |
| 3 | Gemini Sandbox has exercised submit, partial fill, cancel, reject, timeout, and recovery paths. | **Owner-run 2026-09-22 (raw API, Phase 0):** immediate execution, status lookup, partial fill, cancel, and an undersized-order rejection ([capability evidence](phase-0-capability-evidence.md)). **Automated through the adapter:** `test_phase1_gate_gemini_lifecycle.py` covers submit and fill, a working order that partially fills then completes, cancel, reject, a timeout after the venue accepted (recovered by query, never resubmitted), and a timeout before it accepted (submitted exactly once). | Repeat the lifecycle through `GeminiBroker` against the Sandbox, and confirm the documented `trades`/`include_trades` format. A real network timeout is not induced; the status-before-retry rule stands in for it. |
| 4 | Coinbase read-only production calls reconcile cleanly against the account. | **Not yet run.** Supported by automated evidence: paginated accounts (130 accounts over three pages), key-permission reads, and the reconciler. Phase 0 recorded read-only calls but no reconciliation. | **Owner-run.** With the view-only key: read balances and positions, save them as the baseline, reconcile against a second read, and record zero discrepancies (redacted). |
| 5 | Coinbase sandbox fixtures cover authentication, pagination, error payloads, and the documented error scenarios. | **Automated, from documented shapes.** `test_phase1_gate_coinbase_fixtures.py` with `tests/fixtures/coinbase/`: JWT format (ES256, `kid`, `nonce`, per-request `uri`, production and sandbox hosts), refresh once on 401 then fail, pagination of accounts, orders, and fills (unbounded or stuck cursors fail closed), the sandbox's `PostOrder_insufficient_fund`, `CancelOrders_failure`, and `EditOrder_failure` scenarios, `INVALID_ARGUMENT`, bounded 429 backoff, 5xx opening the circuit, and every documented order status. | Capture redacted responses from `api-sandbox.coinbase.com` (no credentials needed) and drop them into the fixture folder; the tests should pass unchanged. |
| 6 | Every order is linked to a signal, strategy version, risk decision, and fill record. | **Automated.** `test_phase1_gate_audit_idempotency.py::test_every_order_links_to_signal_strategy_version_risk_decision_and_fills` (SQLite audit spine) and the lineage checks in every gate scenario; `test_lineage_reports_orders_that_bypassed_the_audit_path` shows the check detects a missing link. | None. |
| 7 | No ambiguous timeout can create a duplicate order. | **Automated.** Ambiguous timeouts are resolved by `client_order_id` in the loop (simulator), through the Gemini and Coinbase adapters (Coinbase searches List Orders, since it documents no client-ID lookup), and after a restart. A lost submission blocks new entries rather than retrying blindly. | None beyond 3. |
| 8 | Every missing or stale risk input fails closed. | **Automated.** Each of the 19 inputs, nulled in turn (`test_risk_execution.py`); every gate blocks an order that breaks it; loop-level refusals for an unreachable broker, a stale or future quote, no market history, and a failing input source (`test_phase1_gate_safety.py`). Risk and execution packages: 100% branch coverage. | None. |
| 9 | Kill switch behavior is verified through all three actuation paths and across a restart. | **Automated.** `test_phase1_gate_safety.py` covers:<br>• dashboard form post without JavaScript;<br>• authenticated API, including refusal of unauthenticated and operator-only re-arm;<br>• flag file read every loop, and the environment flag;<br>• an unreadable flag halts;<br>• each state survives a rebuilt application and switch.<br>**Agent-run on the paper VPS (PR #28, 2026-09-24):** the pause marker persisted across an app restart and a host reboot. | None. |
| 10 | A forced WebSocket disconnect is recovered with gap fill and no unexplained data loss. | **Automated.** `test_phase1_gate_market_data.py`: a scripted disconnect mid-bucket; the missed closed buckets are backfilled from REST; the stored five-minute series validates with no gaps or duplicates; the in-progress bucket is never stored as closed. | **Owner-run/soak.** A real disconnect on the public Coinbase stream and the 72-hour continuous ingest (Phase 1.2 exit). An ingest runner is not yet wired into the service (see follow-ups). |
| 11 | A process restart recovers order and portfolio state correctly. | **Automated.** `test_phase1_gate_reconciliation.py::test_process_restart_recovers_orders_fills_and_portfolio` and `test_startup_recovery.py`. **Agent-run on the paper VPS and the Restart rehearsal workflow (PR #28, 2026-09-24).** | None. |
| 12 | A process killed with `SIGKILL` between the pre-submit persist and the API call recovers with a single position. | **Automated, real process kill.** `test_phase1_gate_audit_idempotency.py` runs `tests/sigkill_child.py` and kills it with `SIGKILL` (Windows: `TerminateProcess`) at two points against a file-backed venue:<br>• **After the venue accepted:** restart resolves the `PENDING_SUBMIT` row by `client_order_id`, records the fill, and leaves one position with one venue submission. It halts once for review, then reconciles cleanly after re-arm.<br>• **Before the venue call:** the venue has no order, recovery halts, and a retry submits exactly once. | None. |
| 13 | With the database unavailable, no order is submitted, the system halts, and it recovers cleanly when the database returns. | **Automated.** `test_phase1_gate_audit_idempotency.py` covers a full outage, a connection lost between the decision and the order record, and one lost after the broker call. In each, trading halts before or without an unrecorded order; restart recovery resolves it once the database returns; trading resumes after an operator re-arm. | Optional: a Postgres-outage phase in `deploy/drill.sh`. |
| 14 | The scheduled reconciler runs unattended for at least seven days. Injected divergences in positions and in balances each produce a discrepancy event, an operator alert, and the configured safety trip, and the broker's record survives correction. | **Automated.** `test_phase1_gate_reconciliation.py`:<br>• reconciliation stays clean while the loop trades;<br>• an injected position divergence and an injected balance divergence each produce a persisted discrepancy, a phone-push alert, and `HALTED`; the broker's state becomes the saved baseline and the next run is clean;<br>• the loop survives a broker outage;<br>• the service starts the scheduler when a broker is configured. | **Owner-run/soak.** Seven days unattended requires a configured broker (`BROKER_PROVIDER=gemini-sandbox` in paper) on the VPS. |
| 15 | `GeminiBroker` raises at construction against any non-sandbox host, and the host is not settable from configuration. | **Automated.** `test_phase1_gate_safety.py`: seven hostile or production URLs rejected; host environment variables ignored; the adapter module never imports `os`. | None. |
| 16 | Backtest/replay parity is demonstrated on at least three windows, including one high-volatility window. | **Automated.** `test_phase1_gate_modes.py`: a calm trend, a calm range, and a high-volatility window (bar ranges up to about 7.8% versus about 1%). Fills and final equity agree to the cent, with fees, spread, and slippage. Under tighter limits every difference is a recorded refusal. | Optional during the soak: repeat on recorded Coinbase windows. |
| 17 | The application cannot enter live mode without the explicit confirmation flag and a trade-capable Coinbase key. | **Automated.** The startup guard needs `LIVE_CONFIRMATION` and `CREDENTIAL_SCOPE=trade`. Live mode now also needs `BROKER_PROVIDER=coinbase`. Before recovery, the service asks Coinbase for the key's permissions and refuses a key that cannot trade or can transfer funds. | Phase 2 will exercise it with the real trade key. |

## Defects found by the gate

Each was fixed in this card, with a test that fails on the old behavior.

**Coinbase adapter** (`brokers/coinbase.py`), checked against the Advanced Trade API reference on 2026-09-24:

- **Account pagination.** Accounts were read from one page only. List Accounts returns 49 per page by default, and the Phase 0 capture saw exactly 49 accounts, so balances were very likely truncated. The adapter now reads every page, with a bounded page budget.
- **Order and fill lookups.** Orders and fills were requested on undocumented paths (`/orders/client:{id}`, `/orders/{id}/fills`). They now use `/orders/historical/{id}` and paginated `/orders/historical/fills`. A timed-out order, which has no venue ID, is found by searching List Orders, because Coinbase documents no lookup by client order ID.
- **Sign-in token.** The JWT `uri` omitted `/api/v3/brokerage`, the header lacked `kid` and `nonce`, and one cached token was reused across paths. Every private call would have been rejected.
- **Create Order.** The request omitted the required `side`. The response carries no fill state, so the order is now read back.
- **Cancel and edit failures.** A failed cancel was reported as success. Edit handling overwrote the order-ID mapping.
- **Error mapping.** An HTTP 400/422 on create is now a rejection, not a retryable error. `UNKNOWN_ORDER_STATUS` now maps to `UNKNOWN`, not `OPEN`.

**Gemini adapter** (`brokers/gemini.py`): fills were read from a `fills` field that Gemini does not send, so they would never have been recorded. Order Status now requests `include_trades`, reads `trades` and `fee_amount`, and sends `order_id` as an integer. Rejections carry Gemini's `reason`.

**Market data** (`data/stream.py`): the candles channel sends five-minute buckets updated every second. Every update was forwarded as a closed one-minute bar, which would have produced constant false gaps and stored half-finished bars. Buckets are now emitted only after they close, and gap fill stops at the current bucket.

**Risk engine** (`risk/engine.py`): the exposure gates (open positions, per-symbol position, allocation, cash reserve) treated a sell as a buy. Near a limit, that could block the sale that reduces exposure. Sells are now checked only against the held position, and a sell larger than the position is refused because shorting is unsupported.

**Kill switch** (`risk/kill_switch.py`): once the file and environment flags are read every loop, a flag left at `running` would have undone an operator's emergency stop. The flags can now only pause or halt, and an unreadable or unrecognised flag halts. Re-arming stays on the authenticated admin path.

**Backtester** (`strategy/backtest.py`): a signal sell executed at the next bar's close, not its open, contrary to the documented timing contract.

**Audit spine:** signals and risk decisions were never persisted, so an order's `risk_approval_id` pointed at nothing. The unused `risk/approval.py` helper, which could create an approved `RiskApproval` without evaluating any gate, was removed.

## Open items and follow-ups

These are decisions or later work, not criteria this card can close.

1. **Paper runtime wiring (before the Phase 1.5 soak, #12).** The live market-data ingest feeding `TradingCycle` is not yet started by the deployed service. The soak needs it, along with the 72-hour ingest run.
2. **Gemini market orders.** Gemini's documentation lists `exchange market` as a type but also says market orders are not directly supported. It recommends an immediate-or-cancel limit order with an aggressive price, which is what the Phase 0 capture used. Confirm in the Sandbox; if `exchange market` is refused, a price-collar decision is needed.
3. **Coinbase key algorithm.** The Coinbase documentation read on 2026-09-24 says only ES256 (ECDSA) keys are supported, and the adapter signs ES256. The Phase 0 capture, however, signed successfully with an Ed25519 key, which this adapter cannot use. Before the read-only check, confirm which key type Coinbase accepts for Advanced Trade and issue an ECDSA view-only key if needed.
4. **Cost basis on spot venues.** Coinbase and Gemini positions carry no average price, while the reconciler compares it. A paper runtime trading on Gemini would diverge on its first fill. Decide whether positions reconcile on quantity when a venue reports no cost basis. Relatedly, risk inputs refuse when a held asset has no price mark, which Coinbase dust balances would trigger.
5. **Never-submitted orders.** A `PENDING_SUBMIT` order the venue has no record of blocks new entries until an operator resolves it, and there is not yet an operator control to close it.
6. **Risk-limit state across restarts.** Daily-loss and drawdown baselines are held in memory and restart with the process.
7. **Coinbase pre-submit lookup cost.** Before every submission, the execution engine asks the venue whether the `client_order_id` already exists. Coinbase has no direct lookup, so each check searches up to ten pages of order history. Bound the search by the order's creation time before Phase 2 places real orders.

## Owner-run verification still required

AGENTS.md keeps exchange and provider checks owner-run. What remains:

1. **Gemini Sandbox (Sandbox key):** the lifecycle in 3 and the loop in 1 through `GeminiBroker`, and the market-order question in open item 2.
2. **Coinbase production, read-only (view-only key):** the reconciliation in 4, after confirming the key type in open item 3.
3. **Coinbase Advanced Trade sandbox (no key):** capture the fixtures in 5.
4. **Soak (#12):** seven days of scheduled reconciliation with a configured broker (14), a real stream disconnect, and the 72-hour ingest (10).

## Validation

**Local (Windows, Python 3.14, isolated worktree):**
- Ruff format and lint: pass.
- mypy (47 source files): pass.
- pytest: 256 passed, none skipped; 94.0% line coverage (the CI measure, 80% required), 91.7% counting branches.
- Branch coverage: 100% on `risk/`, `execution/`, `app/trading.py`, and `portfolio/ledger.py`.
- `git diff --check`: pass.

**Remote CI:** reported on the pull request for its exact head commit.
