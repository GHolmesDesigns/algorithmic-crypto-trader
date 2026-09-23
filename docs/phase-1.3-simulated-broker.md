# Phase 1.3: Simulated broker and shared contract

`brokers.simulated.SimulatedBroker` is the deterministic execution engine for
replay and paper-mode tests. It never contacts an exchange.

The simulator executes market orders at the quote's far side, applies a
configured fractional slippage and fee rate, and records fills, balances, and
positions using `Decimal`. Marketable limit orders use a deterministic
cumulative schedule (50% and then 100% by default); non-marketable limits stay
open until a later quote makes them eligible and `advance()` is called.

`FaultPlan` injects provider-like behavior at a named operation: rejects,
ambiguous timeouts, duplicate acknowledgements, out-of-order fills, HTTP 429
rate limits, and total unavailability. A timeout persists an `UNKNOWN` order;
callers must query the persisted `client_order_id` before retrying. A retry
returns the existing order and cannot create a duplicate.

The reusable assertions in `tests/contracts/broker_contract.py` are the
provider-neutral contract. Adapter tests should call
`assert_shared_broker_contract` unchanged and add only adapter-specific
capability checks. Fault behavior is tested in `tests/test_broker.py` without
provider credentials or network access.
