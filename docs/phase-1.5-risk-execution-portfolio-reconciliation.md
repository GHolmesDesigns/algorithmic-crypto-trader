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

## The app's own fills and the venue's balance precision

A fill's notional, quantity × price, can carry more decimals than the venue reports a balance in. The ledger projected USD exactly and the reconciler compared for equality, so the app's own market order halted trading about four minutes after it filled (#118). The owner recorded this on the Gemini Sandbox, where each halt named only **balance, USD, available**; the BTC position, the fills, the order status and the USD hold all matched:

| Order | Quantity × price | Fee as displayed | Projected USD change | Sandbox change, 5 decimals |
| --- | --- | --- | --- | --- |
| Buy, 2026-10-05 12:00:13 UTC | 0.0001 × 85328.82 = 8.532882 | 0.03413 | -8.567012 | -8.56701 |
| Sell, 2026-10-06 10:45:19 UTC | 0.0001 × 85259.74 = 8.525974 | 0.0341 | +8.491874 | +8.49187 |

The rule that replaced it:

- A broker declares the unit it rounds an asset's balance to in `BrokerCapabilities.balance_increments`. Only the Gemini Sandbox declares one, `USD` at `0.00001`. A broker that declares nothing, which today means Coinbase and the simulator, is compared exactly, as before.
- `portfolio.ledger.apply_fills` settles every amount a fill moves in a declared asset, its quantity × price and its fee, in that unit: each is cut toward zero, fill by fill, before it is added. The projection is also checked unrounded, so a balance that is negative at full precision, or only as settled, still refuses. An undeclared asset, holds, positions, fills and order status are not rounded.
- The comparison is still equality. Both sides now have the venue's precision; no tolerance was added, so a difference of one cent, or of one unit of the declared precision, still halts. This keeps the decision in #43.
- The unit is declared by the adapter, never inferred from a reported value. A venue that prints a balance of 10000.00 as `10000` would otherwise look as if it reported whole dollars, and the cent difference that matters would be rounded away.

### Why fill by fill, toward zero

The first version of this rule rounded the projected balance once, half up. It fitted the two trades above, which left open whether the Sandbox rounds or truncates and whether it rounds each fill or the total. The sell of 2026-10-06 20:55 UTC answered both: it filled in two pieces, and the app halted on a USD gap of `0.00002`.

| Trade, 2026-10-06 UTC | Quantity × price | Fee | Sandbox USD after |
| --- | --- | --- | --- |
| Buy, 19:40 | 0.0001 × 84665.9 = 8.46659 | 0.03386 | 9991.42441 |
| Sell, 20:55, first piece | 0.000062 × 84390.47 = 5.23220914 | 0.02092 | |
| Sell, 20:55, second piece | 0.000038 × 84293.86 = 3.20316668 | 0.01281 | 9999.82604 |

The Sandbox credited 5.23220 and 3.20316, each piece cut to 5 decimals. Rounding the total once, half up, projected 9999.82606; cutting the total once projected 9999.82605. Against all four recorded trades, only cutting each fill toward zero matches every balance; rounding half up or half even, per fill or on the total, and rounding up each fail at least one. The sell side is proven by remainders of half a unit or more. The buy side is inferred: the one recorded buy with a remainder had `0.000002`, which every rule but rounding up agrees on. If a buy settles differently, it halts by one unit, and `drill.sh diagnose` shows the gap.

`tests/test_balance_precision.py` pins the recorded trades, the split sell, a partial fill, and the cases that must still halt.

## Validation boundary

The automated suite uses only `SimulatedBroker`, deterministic fixtures, and temporary local state. No provider credentials, exchange writes, or live trading are used. Owner-run provider verification is not required for this broker-independent safety layer; adapter and end-to-end evidence remains deferred to Phase 1.6 and the Phase 1 gate.
