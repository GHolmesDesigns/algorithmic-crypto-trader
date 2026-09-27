# Phase 1.5 risk, execution, portfolio, and reconciliation

This package adds the safety boundary required before any order-capable venue adapter.

## Contracts

- `risk.engine.evaluate` evaluates the gates in order and returns a `RiskApproval`; any absent or unsafe input refuses the order.
- `execution.ExecutionEngine` writes a `PENDING_SUBMIT` record before calling a broker. The deterministic `OrderRequest.client_order_id` is the idempotency key and the store enforces uniqueness.
- Unknown or pending submissions are resolved by querying the broker by client order ID before a retry. A persistence failure raises before any broker call.
- `risk.kill_switch.KillSwitch` persists `RUNNING`, `PAUSED`, and `HALTED`, reads environment/file actuation, and does not automatically re-arm `HALTED`. The flags can pause or halt but never re-arm, and an unreadable flag halts ([Phase 1 gate](phase-1-gate-acceptance.md)).
- The exposure gates limit what a buy adds; a sell is checked only against the held position, and shorting is refused.
- `portfolio.reconciliation.Reconciler` treats broker orders, fills, positions, and balances as authoritative. Any divergence emits an alert callback and trips the kill switch before new entries.
- The operator endpoints are authenticated and work as ordinary POST form actions, so they do not depend on JavaScript.

## Orders the app did not place

The reconciler compares each balance's `available` and `hold` exactly. The expected state from `portfolio.ledger.apply_fills` moves `available` only for fills the app recorded. It carries `hold` over from the last broker snapshot. Any account activity the app did not place therefore halts trading at the next scheduled reconciliation:

- **A resting limit order:** before anything fills, the venue moves a sell's coins, or a buy's dollars plus any fee reserve, from available to hold. Positions count coins on hold (#41), so a resting sell is a balance difference, not a position difference.
- **Its fills and its cancellation:** each one changes the balances again; a cancellation returns the hold to available.
- **A manual trade, or the owner-run Gemini Sandbox check** (`probes/gemini_sandbox_lifecycle.py`), when either runs against the account the app watches.

Each halt raises a `reconciliation_divergence` alert, and the broker's record becomes the new baseline. An order that is still resting and unchanged therefore passes the following run. The app's own orders do not rest: the trading loop sends only market orders, and Gemini caps them as immediate-or-cancel.

This is deliberate. On 2026-09-27 the owner chose to keep the exact check (#43). The rejected alternatives were projecting holds for the app's open orders, whose fee reserves differ by venue, and comparing totals only, which would hide an order the app did not place until it fills. After an expected halt:

1. Wait until nothing the app did not place is left open.
2. Wait until a later run reports `reconciliation.last_result` as `clean` in `GET /operator/state`.
3. Re-arm.

`tests/test_reconciliation.py` pins this behaviour against Gemini and Coinbase account fixtures.

## Validation boundary

The automated suite uses only `SimulatedBroker`, deterministic fixtures, and temporary local state. No provider credentials, exchange writes, or live trading are used. Owner-run provider verification is not required for this broker-independent safety layer; adapter and end-to-end evidence remains deferred to Phase 1.6 and the Phase 1 gate.
