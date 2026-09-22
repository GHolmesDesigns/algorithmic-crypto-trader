# Phase 0 capability evidence

**Evidence date:** 2026-09-22
**Status:** `INCOMPLETE — owner-run verification required`
**Issue:** [#2](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/2)

This sheet separates provider documentation, public unauthenticated observations, and
account-specific owner-run verification. It does not turn documentation into a claim
that an account, jurisdiction, or private API key was tested.

## Safety boundary

- No production credential was read or used.
- No authenticated request, order, transfer, withdrawal, or other provider write was
  performed.
- The Coinbase Advanced Trade requests below were deliberately sent without
  credentials and returned `401`; this is a connectivity/authentication boundary
  observation, not a capability result.
- Gemini observations are public Sandbox market-data reads only. They do not prove
  test-balance provisioning or any order lifecycle behavior.
- Owner-run evidence must be captured with a View-only Coinbase key and a Gemini
  Sandbox key. Secrets, signatures, authorization headers, account identifiers, and
  raw unsanitized payloads must never enter this repository.

## Evidence ledger

| Area | Evidence class | Result on 2026-09-22 | Remaining proof |
| --- | --- | --- | --- |
| Coinbase Advanced Trade product and candle REST calls | Local boundary check | `401 Unauthorized` without a CDP JWT | Owner-run authenticated capture for BTC-USD and ETH-USD |
| Coinbase Advanced Trade accounts | Not run | Requires a signed private request | Owner-run View-only account capture; redact UUIDs and balances as required |
| Coinbase public ticker/candle WebSocket | Documented, not observed | Official WebSocket guide identifies public market-data channels | Owner-run capture of ticker and candle messages with subscription and reconnect notes |
| Coinbase fees, limits, increments, and minimums | Documented baseline only | Current values are account-, product-, or endpoint-specific | Owner-run capture plus source URL/date and jurisdiction |
| Coinbase Advanced Trade Sandbox | Documented baseline only | Sandbox behavior must be checked against the current official guide | Owner-run read-only fixture capture; no order submission |
| Gemini Sandbox symbols and public tickers | Public read observed | `200` from `api.sandbox.gemini.com` for symbols, BTC/USD ticker, and ETH/USD ticker | None for this public slice; it does not satisfy private lifecycle criteria |
| Gemini Sandbox balances and authentication | Not run | Requires a Sandbox account/key | Owner-run balance and signed-auth capture |
| Gemini submit/ack/match/partial-fill/cancel/reject/recovery | Not run | No private order request was attempted | Owner-run non-production lifecycle, with every state and recovery path recorded |
| Agreements, automated-trading restrictions, and geographic eligibility | Not determined | These are owner/jurisdiction-specific legal and account checks | Owner review of current Coinbase and Gemini terms and account eligibility |

## Coinbase Advanced Trade

| Capability | Current disposition | Required evidence |
| --- | --- | --- |
| CDP Ed25519/JWT authentication | `owner-run-pending` | View-only key, successful signed request, key scope, timestamp, and redacted response |
| Products: BTC-USD and ETH-USD | `owner-run-pending` | Product IDs, status, quote/base currencies, increments, minimums, and response date |
| Historical candles | `owner-run-pending` | At least one bounded BTC-USD and ETH-USD response; preserve interval, range, and redacted payload |
| Accounts | `owner-run-pending` | View-only account response with UUIDs and balances redacted or bucketed |
| Public ticker/candle WebSocket | `documented-not-observed` | Subscription request, at least one message of each type, heartbeat/reconnect result |
| Fees and rate limits | `owner-run-pending` | Account fee tier, documented/request limits, HTTP headers, and source date |
| Sandbox | `documented-not-observed` | Current official behavior and a read-only fixture; do not treat it as a market simulator |

The public Coinbase Exchange candle endpoint was intentionally not used as Advanced
Trade evidence. Its successful response would describe a different API surface and
must not be substituted for the authenticated Advanced Trade capture.

## Gemini Sandbox

The official Sandbox guide documents a separate REST host,
`https://api.sandbox.gemini.com`, a WebSocket host, and test-only balances. On this
date, public market-data reads returned successfully for the symbols list and the
BTC/USD and ETH/USD tickers. The redacted responses are kept in
[`phase-0/fixtures/`](phase-0/fixtures/).

The following remains unverified and is required before this card can be marked
complete:

1. Sandbox account provisioning and starting balances.
2. Signed authentication and permission boundaries.
3. Submit and acknowledge.
4. Working, match, partial fill, and terminal fill.
5. Cancel, reject, timeout, and recovery after an ambiguous response.
6. Any observed divergence from production behavior that affects the planned adapter.

No order lifecycle claim is made here because no private Sandbox credential was
available for owner-run verification.

## Draft payload model and redaction rules

The fixture files are deliberately limited to non-secret public observations and
schema-only examples. They are not substitutes for owner-run account evidence.
Each captured response should be reduced to these draft records before committing:

```text
ProviderObservation {
  provider, environment, endpoint, observed_at_utc,
  http_status, request_shape, response_shape, source_url,
  evidence_class, notes
}

MarketObservation {
  provider, environment, product, observed_at_utc,
  bid, ask, last, candle_interval, candle_rows,
  source_observation_id
}

OrderLifecycleObservation {
  provider, environment, client_order_id_hash,
  requested_state, observed_states, terminal_state,
  recovery_result, observed_at_utc, source_observation_id
}
```

Before saving a fixture, remove or replace API keys, private keys, JWTs, signatures,
authorization headers, account and portfolio UUIDs, email addresses, IP addresses,
client order IDs, and raw balances where they identify the account. Keep prices,
quantities, status values, timestamps, increments, and error codes when they are
needed to explain capability behavior.

## Owner-run capture checklist

The owner should run the bounded capture from a controlled environment and attach the
redacted results to this sheet:

- Coinbase: View-only CDP key; no trade, transfer, or withdrawal permission. Capture
  authentication, products, candles, accounts, fee tier, limits, and public WebSocket
  messages. Verify the key scope in the portal and record the account/jurisdiction
  context without naming the account.
- Gemini: Sandbox-only key. Confirm the host is `api.sandbox.gemini.com` or the
  documented Sandbox WebSocket host before every private request. Capture balances,
  auth, and the complete non-production order lifecycle. Use a bounded request budget
  and clean up in `finally`.
- For both: record UTC time, endpoint, status, source URL, redaction decision, and
  whether the result was documented, public-observed, or owner-run.

## Sources

All sources must be rechecked at the owner-run capture date because exchange behavior,
fees, limits, terms, and eligibility can change.

- [Coinbase Advanced Trade overview](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/overview)
- [Coinbase Advanced Trade Sandbox](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox)
- [Coinbase Advanced Trade WebSocket guide](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/websocket)
- [Coinbase Advanced Trade Python SDK](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sdk)
- [Coinbase Advanced Trade API reference](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api)
- [Coinbase App API terms](https://www.coinbase.com/legal/user_agreement/united_states)
- [Gemini Sandbox](https://developer.gemini.com/get-started/sandbox)
- [Gemini REST API](https://docs.gemini.com/rest-api/)
- [Gemini WebSocket streams](https://developer.gemini.com/trading/websocket/streams)
- [Gemini user agreement](https://www.gemini.com/legal/user-agreement)

## Acceptance disposition

This document is a safe, reviewable Phase 0 evidence scaffold with dated public
observations. Issue #2 is **not complete** until the owner-run Coinbase and Gemini
private checks above are performed and redacted evidence is added. Merging this
documentation PR must not be interpreted as authorization to enable live trading.
