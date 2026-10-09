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

## Filled orders with delayed fills

On 2026-10-07, Gemini Sandbox acknowledged an app sell as filled, but the immediate
`order/status` read returned 404. The execution store correctly kept the order
`pending_submit` with no fill. The next scheduled reconciliation skipped the
terminal order and halted on the missing fill and resulting exact balance and
position differences. The next trading cycle recovered the fill, and subsequent
reconciliations were clean (issue #137).

Before each comparison, the scheduler now refreshes every tracked open order and
every tracked filled order whose recorded fill quantities total less than the
venue-reported filled quantity. Recovery persists newly readable fills before the
ledger projects balances. A fully recorded filled order needs no refresh. When
an accepted or recovered order still lacks fills, execution logs a redacted
`order step=fill ... result=pending` line with filled and recorded quantities, and
its durable row stays pending. If fills remain unreadable at reconciliation,
the exact comparison still diverges, alerts, and halts. This rule does not add a
tolerance, skip a run, or change the settlement rule from #133.

The local tests replay the delayed read and the persistent lag, plus partial and
complete fill quantities and an adapter-level 404. Sandbox timing cannot be forced;
provider behavior remains unverified until a later natural lag event shows the
pending log line followed by a clean reconciliation.

## Failed pre-submit lookup on a new order

The execution engine saves each approved order before looking it up by
`client_order_id`. If that lookup fails during the same call that created the
saved row, the engine knows it has not called `submit_order`; the order was
never sent. It closes the row as `canceled` and writes the same
`order_closed_never_received` event used by the administrator close, with actor
`system`, the lookup failure as the reason, and the signal, strategy version,
and risk-decision links preserved. The operator receives one warning alert
through the configured destinations.

That cycle still reports a broker error. The kill switch stays unchanged, and
the next cycle creates a new signal and risk decision. The closed order cannot
be resubmitted. A row that existed when this call began remains unresolved and
follows recovery and the five-minute operator-review halt. If `submit_order`
itself fails or times out, the order remains `unknown` for recovery because the
venue may have received it. These paths have deterministic simulator coverage;
provider timing and alert delivery remain unverified until an owner-run
observation.

### Repeated lookup failures halt trading (#145)

Closing a never-sent order stops one slow lookup from halting trading, but it
would also let a status endpoint that stays down close an order on every signal
without end. The execution engine therefore counts never-sent closes in a row,
and a lookup that works, whether it finds the order or not, resets the count.
When the third close in a row happens, the trading loop halts through the
normal halt path, naming the count and the last failure type in the reason, and
sends the usual halt alert. All three orders are already closed, so nothing is
left pending; the operator re-arms after checking the venue. The count starts
over after the halt, so a re-arm gets a fresh allowance. A lookup failure on a
row that already existed, and a failure after `submit_order` starts, change
nothing in the count. The threshold is the constant
`NEVER_SENT_HALT_THRESHOLD` (3) in `app/trading.py`; it is not an environment
setting.

## Validation boundary

The automated suite uses only `SimulatedBroker`, deterministic fixtures, and temporary local state. No provider credentials, exchange writes, or live trading are used. The delayed-fill fix has adapter-level fixture coverage, but its real Sandbox timing remains owner-run verification when a natural lag event occurs. Other adapter and end-to-end evidence remains in Phase 1.6 and the Phase 1 gate.
