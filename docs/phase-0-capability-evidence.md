# Phase 0 capability evidence

**Evidence date:** 2026-09-22
**Status:** `COMPLETE — owner account eligibility acknowledged`
**Issue:** [#2](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/2)

This sheet separates provider documentation, public unauthenticated observations, and
account-specific owner-run verification. The owner-run record is redacted and does not
contain credentials, account identifiers, balances, or order identifiers.

## Safety boundary

- Coinbase was used only for authenticated read-only requests. No Coinbase order,
  transfer, withdrawal, or other provider write was performed.
- Gemini requests were restricted to `https://api.sandbox.gemini.com`; the observed
  order writes were Sandbox-only and used a Sandbox credential.
- Secrets, signatures, authorization headers, account identifiers, balances, order
  identifiers, client order IDs, and raw unsanitized payloads are omitted.

## Evidence ledger

| Area | Evidence class | Result on 2026-09-22 | Remaining proof |
| --- | --- | --- | --- |
| Coinbase Advanced Trade product and candle REST calls | Owner-run read-only | Products and bounded BTC-USD/ETH-USD candle responses returned `200` | Recheck when account or product changes |
| Coinbase Advanced Trade accounts and key permissions | Owner-run read-only | Accounts and key-permission requests returned `200`; `can_view=true`, `can_trade=false`, `can_transfer=false`; identifiers and balances were redacted | Confirm portal scope when the key changes; withdrawal scope was not returned by this endpoint |
| Coinbase public ticker/candle WebSocket | Owner-run public | Ticker and candle messages observed without credentials | Recheck reconnect behavior during adapter implementation |
| Coinbase fees, rate limits, increments, and minimums | Owner-run read-only | Fee tier, fee rates, rate-limit header names, product increments, and minimums captured | Recheck when fee tier or API contract changes |
| Coinbase Advanced Trade Sandbox | Documented baseline only | Sandbox behavior must be checked against the current official guide | Owner-run read-only fixture capture; no order submission |
| Gemini Sandbox symbols and public tickers | Public read observed | `200` from `api.sandbox.gemini.com` for symbols, BTC/USD ticker, and ETH/USD ticker | None for this public slice; it does not satisfy private lifecycle criteria |
| Gemini Sandbox balances and authentication | Owner-run Sandbox | Signed balance request returned `200` with six asset records; values were redacted | Recheck when Sandbox account scope changes |
| Gemini submit/ack/match/partial-fill/cancel/reject/recovery | Owner-run Sandbox | Immediate execution, status query, partial fill, explicit cancellation, and undersized rejection were observed | An actual ambiguous-timeout response was not induced; status-before-retry remains the required recovery rule |
| Agreements, automated-trading restrictions, and geographic eligibility | Public terms reviewed; owner confirmed | Current Coinbase US agreement and Gemini user-agreement pages were reviewed; the owner confirmed KYC verification in the United States | Recheck if the account, jurisdiction, or agreement changes |

## Coinbase Advanced Trade

| Capability | Current disposition | Required evidence |
| --- | --- | --- |
| CDP Ed25519/JWT authentication | `owner-run-complete` | Ed25519 key parsed and signed read-only requests returned `200` |
| Products: BTC-USD and ETH-USD | `owner-run-complete` | Both products returned `200` with online status, increments, and minimums |
| Historical candles | `owner-run-complete` | Bounded one-hour BTC-USD and ETH-USD responses returned `200` with three rows each |
| Accounts and key permissions | `owner-run-complete-redacted` | Account and permission responses returned `200`; view enabled, trading and transfers disabled; identifiers and balances omitted |
| Public ticker/candle WebSocket | `owner-run-complete` | Public ticker and candle messages observed on the Advanced Trade endpoint |
| Fees and rate limits | `owner-run-complete` | Fee tier/rates and rate-limit header names captured |
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

The following remains unverified and should remain explicit follow-up work:

1. An actual network timeout or ambiguous response and its recovery procedure.
2. Any observed divergence from production behavior that affects the planned adapter.

The captured Sandbox run proved signed authentication, test-balance visibility,
immediate execution, status lookup, partial fill, explicit cancellation, and an
undersized-order rejection. It did not induce a real ambiguous network response.

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

The owner-run capture is attached as
[`owner-run-2026-09-22.json`](phase-0/fixtures/owner-run-2026-09-22.json). Future
captures should follow the same bounded and redacted process:

- Coinbase: View-only CDP key; no trade, transfer, or withdrawal permission. Capture
  authentication, products, candles, accounts, fee tier, limits, and public WebSocket
  messages. Verify the key scope in the portal and record the account/jurisdiction
  context without naming the account.
- Gemini: Sandbox-only key. The captured run confirmed the REST host before every
  private request, captured balances and signed auth, executed one tiny IOC order,
  queried its status, observed a partial fill, created and canceled one live limit
  order, and confirmed an undersized-order rejection. No network timeout was induced.
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

This document is a safe, reviewable Phase 0 evidence record with dated public and
owner-run observations. The owner has acknowledged the applicable United States
account context. A real ambiguous timeout was intentionally not induced; the
status-before-retry recovery rule remains the required implementation behavior.
Merging this documentation PR must not be interpreted as authorization to enable
live trading.
