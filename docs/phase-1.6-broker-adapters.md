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
- An adapter whose venue settles a balance in a fixed unit declares the unit in
  `BrokerCapabilities.balance_increments`. Gemini declares `USD` at 5 decimals
  and Coinbase declares none, so only Gemini's fills are settled in that unit,
  one by one, before reconciliation ([why](phase-1.5-risk-execution-portfolio-reconciliation.md#the-apps-own-fills-and-the-venues-balance-precision)).

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

## Coinbase order search (#152)

Coinbase documents no lookup of an order by `client_order_id`, so the adapter
lists orders (`GET /orders/historical/batch`) and matches on `client_order_id`.
That search used to read up to ten pages of 250 orders, newest first, for every
new order, and reported "no such order" when the ten pages ran out with history
still unread.

**What Coinbase documents** (read on 2026-10-09 from the
[List Orders](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-orders)
and
[Create Order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order)
reference pages; no live call was made):

- `start_date` and `end_date` are RFC 3339 timestamps on an order's creation
  time: "The start date to fetch orders from (inclusive)" and "The end date to
  fetch orders from (exclusive)". Each response has `has_next` and a `cursor`.
- Create Order says that if a `client_order_id` "is not unique, the order will
  not be created" and the order with that ID "will be returned instead".

**What the pages do not say**, so the code does not rely on it: whether the date
filters apply to open orders, a maximum `limit` or a default page size, a
maximum number of orders or date range, the sort direction, how long or in what
scope a client order ID is remembered, the HTTP status of a duplicate, and what
`DUPLICATE_CLIENT_ORDER_ID` means.

**What the adapter does now**

- `ExecutionEngine` registers its order store with an adapter that offers
  `use_saved_orders`, so the search can read an order's saved creation time and
  product. A new order's pre-submit search starts five minutes before its
  creation time (a margin for clock difference) and names its product: one
  request. After a restart the search starts at the order's real saved creation
  time. An order with no saved record, or a store that cannot be read, gets the
  full listing, never a narrower one.
- The search returns "no such order" only when the listing reached its end. If
  the ten-page budget runs out, or Coinbase reports more pages without a
  cursor, it raises `ProviderHTTPError` 502 and nothing is submitted. The
  engine then closes a new order as never sent (#143), so an unreadable search
  does not halt trading by itself.
- Duplicate protection at Coinbase is documented but its scope and retention are
  not, so the adapter does not depend on it to make a late submission safe.

Live behavior of the time filter is unverified until an owner-run read-only
List Orders request is recorded here.

## Phase 1 gate corrections

The [Phase 1 gate](phase-1-gate-acceptance.md) checked both adapters against
the providers' documentation on 2026-09-24 and corrected their wire formats:

- **Coinbase:**
  - reads every page of accounts;
  - uses the documented historical order and fill endpoints, and searches
    List Orders for a timed-out order;
  - builds per-request JWTs with the full path, `kid`, and `nonce`;
  - sends `side` on create and reads the order back for its fill state;
  - treats failed cancels and edits as failures;
  - checks key permissions for live mode.
- **Gemini:** requests `include_trades` and reads `trades` and `fee_amount`.

## Local validation

The repository preflight remains the required CI gate: Ruff format, Ruff lint,
mypy, and pytest with an 80% coverage floor. Provider credentials are not read
by the test suite.
