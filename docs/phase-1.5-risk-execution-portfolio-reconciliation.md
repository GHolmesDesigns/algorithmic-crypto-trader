# Phase 1.5 risk, execution, portfolio, and reconciliation

This package adds the safety boundary required before any order-capable venue adapter.

## Contracts

- `risk.engine.evaluate` evaluates the gates in order and returns a `RiskApproval`; any absent or unsafe input refuses the order.
- `execution.ExecutionEngine` writes a `PENDING_SUBMIT` record before calling a broker. The deterministic `OrderRequest.client_order_id` is the idempotency key and the store enforces uniqueness.
- Unknown or pending submissions are resolved by querying the broker by client order ID before a retry. A persistence failure raises before any broker call.
- `risk.kill_switch.KillSwitch` persists `RUNNING`, `PAUSED`, and `HALTED`, reads environment/file actuation, and does not automatically re-arm `HALTED`.
- `portfolio.reconciliation.Reconciler` treats broker orders, fills, positions, and balances as authoritative. Any divergence emits an alert callback and trips the kill switch before new entries.
- The operator endpoints are authenticated and work as ordinary POST form actions, so they do not depend on JavaScript.

## Validation boundary

The automated suite uses only `SimulatedBroker`, deterministic fixtures, and temporary local state. No provider credentials, exchange writes, or live trading are used. Owner-run provider verification is not required for this broker-independent safety layer; adapter and end-to-end evidence remains deferred to Phase 1.6 and the Phase 1 gate.
