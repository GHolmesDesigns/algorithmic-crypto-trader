# Phase 1.6 broker adapters

This card adds the provider-specific execution boundary after the risk,
execution, and reconciliation layer. `CoinbaseBroker` targets Coinbase
Advanced Trade, while `GeminiBroker` is permanently restricted to the Gemini
Sandbox host. Both implement the unchanged `BrokerInterface` and use `Decimal`
domain models.

## Safety and recovery contracts

- Coinbase private requests use a short-lived JWT. A 401 invalidates the
  cached token and permits one refresh; repeated authentication failure is
  returned to the caller.
- Gemini signs every private request with the Sandbox API key and HMAC secret.
  The adapter validates the configured URL at construction and rejects every
  host that does not end in `.sandbox.gemini.com`; the request base URL is then
  set to the internal Sandbox constant.
- Both adapters apply the shared token bucket, bounded 429 backoff, and
  circuit breaker before provider calls. The circuit health is exposed for the
  risk engine.
- A transport timeout during submission produces an `UNKNOWN` order attached
  to `AmbiguousSubmissionError`. A repeated submission first queries the
  persisted `client_order_id`; it does not blindly submit a second order.
- Coinbase native editing is limited to open limit orders whose replacement
  quantity is not below the filled quantity. Gemini advertises no native edit.
  Requests outside Coinbase's native bounds require a fresh `RiskApproval` and
  use cancel-and-replace.
- Authenticated Coinbase user-order events are supported, with bounded status
  polling available as the recovery path.

## Automated evidence

`tests/test_broker_adapters.py` runs the unchanged shared contract against both
adapters using `httpx.MockTransport`; it never contacts an exchange. The
fixtures cover:

| Capability | Evidence |
| --- | --- |
| Quote and authenticated request shape | Coinbase and Gemini contract tests |
| Submit, acknowledgement, fill, and duplicate idempotency | Shared contract tests |
| Ambiguous timeout and status-before-retry recovery | Coinbase timeout test |
| Gemini Sandbox host refusal | `test_gemini_rejects_every_non_sandbox_host` |
| Deterministic client order IDs and fill normalization | Shared contract tests and adapter mappers |

The automated suite is wire-contract evidence only. Owner-run verification is
still required before treating this as provider capability evidence: Gemini
Sandbox must exercise working, partial-fill, fill, cancel, reject, timeout,
and recovery states; Coinbase must perform read-only production reconciliation
with a view-only credential. No automated test places an order or contacts a
production exchange.

## Local validation

The repository preflight remains the required CI gate: Ruff format, Ruff lint,
mypy, and pytest with an 80% coverage floor. Provider credentials are not read
by the test suite.
