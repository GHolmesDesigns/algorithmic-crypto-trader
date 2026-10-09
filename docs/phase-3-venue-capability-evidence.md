# Phase 3 venue capability evidence

**Evidence date:** 2026-10-09
**Status:** `DOCUMENTED AND PUBLIC-OBSERVED — OWNER-RUN AUTHENTICATED EVIDENCE NOT YET COLLECTED`
**Issue:** [#79](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/79)
**Feeds:** [#14](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/14), the Phase 3 venue decision

This sheet records what is known about Kraken Pro, Binance.US, OKX US and Robinhood Crypto as **spot** venues, with every finding labelled by how it was obtained. It does not choose an integration order, approve an adapter, or enable anything.

An account's existence is not proof of API trading. Nothing in this sheet is account-specific: no owner account, key, balance, order or identifier was used, so every per-account question is `unverified` until the owner-run view-only probes in this document are run.

## Evidence classes

| Class | Meaning | Where it appears |
| --- | --- | --- |
| **Documented** (`D`) | Stated on the venue's official documentation, help center or notice page, read on 2026-10-09. The page and, where useful, its own date are named. | Every venue section |
| **Public observed** (`P`) | An unauthenticated read-only request to a public market-data endpoint, made on 2026-10-09. Reflects the observing network, not any account's eligibility. | Observation log |
| **Owner-run authenticated** (`O`) | A bounded view-only probe run by the owner with the owner's key. None has been run for these four venues. | Probe designs only; every `O` cell is `unverified` |

A result of `unverified` means the item was not found, not read, or conflicts between sources. In later planning an unverified, restricted or unavailable capability is treated as **fail-closed**: it is not assumed to work.

Some official pages were read through a page-reader tool that summarizes text. Where a value matters for a decision (a rate, a fee, a date) it is marked with the page it came from, and conflicts between pages are stated instead of resolved by guess.

## Safety boundary

- No order, cancellation, transfer, withdrawal, deposit, key creation or other write was made, and no key, signature or account was used.
- The only requests to a venue were 9 unauthenticated `GET` requests to public endpoints on 3 hosts (the observation log below), at one request each, with an identifying `User-Agent`. Robinhood has no unauthenticated market data, so it received none.
- No key, account identifier, balance, order identifier or raw private payload is in this repository. Public market metadata (tick sizes, minimums, rate-limit declarations) is.
- Secrets, signatures and example credentials printed in vendor documentation are not reproduced.

## Evidence ledger

| Area | Evidence class | Result on 2026-10-09 | Remaining proof |
| --- | --- | --- | --- |
| Kraken order, order-query, trade-history, rate-limit and release-note pages | Documented | Read in full from `docs.kraken.com`; newest release note is 6 Oct 2026 | Recheck at adapter implementation |
| Kraken public `Time`, `SystemStatus`, `AssetPairs` (BTC/USD, ETH/USD) | Public observed | `200`; status `online`; increments and minimums captured | None for this slice |
| Kraken account permissions, `cl_ord_id` lookup, fills, fees, private WebSocket | Owner-run authenticated | `unverified` | Probe K1 |
| Binance.US API reference and 2026 changelog | Documented | Read from `docs.binance.us`; newest changelog entry 6 Oct 2026; WebSocket API and filters sections not in the readable text | Recheck; probe B1 |
| Binance.US public `ping`, `time`, `exchangeInfo` (BTCUSD) | Public observed | `200`; filters, order types and rate-limit declarations captured | None for this slice |
| Binance.US key scope, order and fill lookup, trading fees | Owner-run authenticated | `unverified` | Probe B1 |
| OKX US API domain, key rules, signing, demo, rate-limit rules, USD→USDC notice | Documented | Read from `okx.com/docs-v5`, the US API FAQ (updated 28 Sep 2026) and the US notice (published 8 Sep 2026) | Recheck; probe O1 |
| OKX US public `time`, `instruments` (BTC-USDC, BTC-USD) | Public observed | `200`; `BTC-USDC` live with `USD` in its quote list; `BTC-USD` returns `51001` (does not exist) | None for this slice |
| OKX US order lookup by client ID, fills, fee rate, WebSocket host | Documented | Not found in the text read: `unverified` | Probe O1 |
| OKX US key scope, order and fill lookup, fees, private WebSocket | Owner-run authenticated | `unverified` | Probe O1 |
| Robinhood Crypto Trading API reference, authentication, rate limits, errors | Documented | Read in full from `docs.robinhood.com/crypto/trading` | Recheck; probe R1 |
| Robinhood help center: API versions, eligibility, fee tiers | Documented | Read; fee-tier table and fee-schedule PDF not read | Probe R1 (fee tier) |
| Robinhood market data, pair tradability, orders | Public observed | Not possible: every endpoint needs a key | Probe R1 |
| Robinhood key scope, `is_api_tradable` pairs, order lookup, executions | Owner-run authenticated | `unverified` | Probe R1 |
| Venue terms, agreements and account eligibility | Documented, with the owner's own account status unconfirmed | Public eligibility statements read per venue below | Owner to confirm each account's state and verification level |

## Capability matrix

`D` = documented, `P` = public observed, `—` = not found, `unverified` = not established. Every row for the owner's own account is `unverified`.

| Capability | Kraken Pro (spot) | Binance.US | OKX US | Robinhood Crypto |
| --- | --- | --- | --- | --- |
| API host | `api.kraken.com` (D, P) | `api.binance.us`; `api1`/`api2` alternates added 6 Oct 2026 (D, P) | `us.okx.com` for accounts registered on `app.okx.com` (D); `openapi.okx.com` "will not work" for US (D) | `trading.robinhood.com` (D) |
| Authentication | Nonce, `API-Sign` header (D) | `X-MBX-APIKEY`, HMAC signature, timestamp, `recvWindow` default 5000 ms, payload percent-encoded before signing since the 1 Sep 2026 change (D) | HMAC-SHA256, `OK-ACCESS-*` headers plus a passphrase, ISO timestamp within 30 s (D) | Ed25519 signature; `x-api-key`, `x-signature`, `x-timestamp`; timestamp valid 30 s (D) |
| Key scopes | Funds, Orders, Addresses, Data groups; separate "Create & modify orders" and "Cancel & close orders"; query-open and query-closed permissions (D) | Enable Read (default), Enable Spot Trading, Enable Withdrawals (D) | Read, Trade, Withdraw (D) | "Select the specific API actions to enable" (D); levels not described |
| IP restriction | IPs or CIDR ranges; key expiry; query date bounds (D) | Bind to trusted IPs; keys unused 90 days and not IP-bound reset to read-only (D) | Up to 20 IPs per key; trade/withdraw keys without an IP binding are deleted after 14 days of inactivity (D) | None documented |
| Client order ID | `cl_ord_id`: UUID, 32-hex short UUID, or free text up to 18 ASCII characters; "uniquely identifies an open order"; exclusive with `userref`; duplicate behavior unstated (D) | `newClientOrderId`: unique among open orders; the same ID is accepted again only after the earlier order has filled; maximum length not found (D) | `clOrdId`: up to 32 alphanumeric characters; unique among pending orders; reusable after a terminal state (D) | `client_order_id`: required, must be a UUID, "for idempotency validation"; duplicate behavior unstated (D) |
| Order lookup by client ID | `QueryOrders` gained a `cl_ord_id` filter on 30 Sep 2026; its schema still marks `txid` required, so a lookup by client ID alone is unconfirmed (D) | `GET /api/v3/order` with `symbol` and `origClientOrderId` (D) | Not found in the text read: `unverified` | No lookup by client ID documented; orders are read by order ID or listed with filters that do not include it (D) |
| Fills | `TradesHistory` (with `ordertxid`, `trade_id`), `QueryTrades`, and `QueryOrders` with `trades` (D) | `GET /api/v3/myTrades` with `symbol`, `orderId`, `fromId`; limit default 500, maximum 1000; a `startTime`/`endTime` span of at most 24 hours (D) | Not found in the text read: `unverified` | An `executions` array on the order; no fills endpoint documented (D) |
| Pagination | Cursor (`with_cursor`, `cursor`) on `TradesHistory`, `ClosedOrders`, `OpenOrders` since 30 Sep 2026; `ofs` deprecated; `limit` 1–100 (D) | `fromId`, `limit`, time ranges (D) | Not read | `cursor` and `limit`; `next` and `previous` links (D) |
| Streaming and private events | WebSocket v2: public `wss://ws.kraken.com/v2`, private `wss://ws-auth.kraken.com/v2`; private `executions` and `balance` channels; token from REST (D) | `listenKey` user-data stream deprecated 17 Sep 2026, retirement targeted Q4 2026; replacement `userDataStream.subscribe.signature` on the WebSocket API with the existing key; WebSocket API URL and stream limits not found in the text read (D) | Public and private WebSocket exist (D); the US host is not stated in the text read: `unverified` | No WebSocket documented: treat as none |
| Rate limits | REST counter per key: Starter 15 (-0.33/s), Intermediate 20 (-0.5/s), Pro 20 (-1/s); ledger and trade-history calls cost 2; `AddOrder` and `CancelOrder` use a separate per-pair limiter (D) | `REQUEST_WEIGHT` 6000/min, `ORDERS` 100 per 10 s and 200000 per day, `RAW_REQUESTS` 300000 per 5 min, limits per IP (D, P) | Per endpoint, per User ID or IP; place order and cancel order 60 requests per 2 s per user and instrument (D) | 100 requests per minute per account, burst 300, token bucket, per-endpoint values may differ (D) |
| Spot pairs and USD quote | `XBT/USD`, `ETH/USD` online (P) | `BTCUSD` trading, spot (P); crypto-only states have no USD services (D) | USD books retired 30 Sep 2026; `Crypto-USDC` pairs now; USD funds convert to USDC; `BTC-USD` does not exist (D, P) | USD pairs only, and only those with `is_api_tradable=true` (D) |
| Increments (observed) | BTC/USD tick 0.1, minimum 0.00005, minimum cost 0.5, 8 volume decimals; ETH/USD tick 0.01, minimum 0.001 (P) | BTCUSD tick 0.01, step 0.00001, minimum quantity 0.00001, minimum notional 1.00 (P) | BTC-USDC tick 0.1, lot 0.00000001, minimum 0.0001, maximum market size 1,000,000 (P) | Not observable without a key |
| Order types | market, limit, iceberg, stop-loss, take-profit and limit variants, trailing stop; `timeinforce` GTC, IOC, GTD, FOK (D) | LIMIT, LIMIT_MAKER, MARKET, STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, TAKE_PROFIT_LIMIT (P) | market, limit, post_only, fok, ioc, optimal_limit_ioc (D) | limit, market, stop_limit, stop_loss (D) |
| Test environment | No self-service Spot sandbox; `validate=true` on `AddOrder` checks an order without trading; a Spot UAT can be requested through an account manager (D) | No testnet found; `POST /api/v3/order/test` validates without matching (D) | Demo trading: `x-simulated-trading: 1`, separate demo keys, separate demo WebSocket hosts; whether it applies to the US domain is not stated (D) | None documented |
| Eligibility (public statements) | Not offered to residents of New York and Maine (D, page dated 30 Mar 2026) | Unsupported: AK, CT, GA, ME, NY, NC, ND, OH, OR, TX, VT, WA and some territories; crypto-only: KS, WI (D, page dated 3 Jun 2026) | US entity OKX INC.; US users may not use foreign OKX products; API needs a verified account (D) | Customers in the United States only; v2 fee-tier orders in "eligible jurisdictions" (D) |
| Fees, as read | See Kraken section; table read once and not independently confirmed | See Binance.US section | See OKX US section; pair group for `-USDC` pairs unknown | 0.00%–0.95% stated; tier table not read |
| Notable 2026 changes | `cl_ord_id` filter and cursors 30 Sep; `FOK` 12 May; `GetApiKeyInfo` 11 Mar | `listenKey` deprecation 17 Sep; 20 s ping 30 Jul; signatures and `exchangeInfo` weight 1 Sep | USD→USDC consolidation 30 Sep | v2 with fee tiers; v1 has no deprecation date |

## Kraken Pro

**Sources read (2026-10-09):** `docs.kraken.com` pages Add Order, Query Orders Info, Get Trades History, Get Tradable Asset Pairs, Spot REST Rate Limits, Spot WebSocket introduction, Release notes, Quickstart; support articles on API keys and the United States quick start.

**Documented**

- `AddOrder` takes `cl_ord_id` as an optional string: a long UUID, a 32-character UUID without dashes, or free ASCII text up to 18 characters. It "uniquely identifies an open order for each client" and cannot be sent with `userref`. The page does not say what happens on a reused ID. `validate=true` checks an order without trading, and the page does not describe that response. `viqc` expresses a market buy's volume in the quote currency. The permission needed is "Orders and trades – Create & modify orders".
- `QueryOrders` accepts up to 50 `txid` values, a `trades` flag, and, since the 30 Sep 2026 release note, a `cl_ord_id` filter. The schema marks `txid` required, so a lookup by client ID alone is not established by the page. Statuses are `pending`, `open`, `closed`, `canceled`, `expired`. A client ID is documented only as unique among open orders, so recovering a **closed** order by client ID is the question to settle first.
- `TradesHistory`: `limit` 1–100 (default 50), cursor pagination, `pair` filter (14 Jul 2026), `ordertxid` and `trade_id` on each trade, `ext_exec_id` (30 Sep 2026). Permission: "Query closed orders & trades".
- Rate limits are per key with a decaying counter; `TradesHistory` and ledger calls cost 2; `AddOrder` and `CancelOrder` are outside the counter and use a per-pair engine limiter shared across REST, WebSocket and FIX. Errors named: `EAPI:Rate limit exceeded`, `EService: Throttled`, `EOrder:Rate limit exceeded`, `EOrder:Orders limit exceeded`.
- WebSocket v2 closes an idle connection after about a minute; a private subscription keeps it open; about 150 connection attempts per rolling 10 minutes per IP before a 10-minute ban.
- Keys: permissions grouped as Funds, Orders, Addresses, Data. Recommended for a first integration: Query Funds, Query Open Orders & Trades, Create & Modify Orders. IP restriction is recommended for production. A nonce window exists and the nonce must always increase; sharing a key across processes causes nonce errors. `GetApiKeyInfo` (added 11 Mar 2026) is described as returning a key's permissions, restrictions and usage and needing no permission; that description comes from a search summary of its reference page, which was not read directly.
- Testing: no self-service Spot sandbox. `validate=true`, a Spot UAT on request through an account manager (separate base URL and keys, not given), or minimum-size live orders.
- Eligibility: not offered to New York and Maine residents (support page last updated 30 Mar 2026). Some newly listed tokens carry additional geographic limits.
- Fees: the Kraken Pro spot table lists tiers by 30-day volume or assets on platform, applying the better one; the page has no date. The reader returned Tier 1 as 0.40% maker and 0.80% taker, Tier 2 at $2.5K as 0.30% and 0.60%, Tier 3 at $10K as 0.22% and 0.38%. These figures are **not independently confirmed** (one reading of one page); use probe K1's `TradeVolume` fee schedule instead of relying on them. Fees default to the quote currency. The `fees` and `fees_maker` fields of `AssetPairs` were deprecated on 8 Sep 2026 and return empty arrays.

**Public observed:** `SystemStatus` `online`. `XBTUSD`: `pair_decimals` 1, `tick_size` 0.1, `lot_decimals` 8, `cost_decimals` 5, `ordermin` 0.00005, `costmin` 0.5, status `online`, venue `international`. `ETHUSD`: `tick_size` 0.01, `ordermin` 0.001, status `online`.

**Open items**

1. **Spot testing (card gap).** There is no self-service way to test an order lifecycle. The choices are `validate`, a UAT through an account manager, or a minimum-size live order. Whether a UAT is available to the owner is `unverified`.
2. Whether `QueryOrders` returns a closed order by `cl_ord_id` alone, and what a reused `cl_ord_id` returns.
3. The balance settlement unit (the app's `balance_increments`) is `unverified`.
4. Whether the owner's Kraken account is `Kraken Pro` enabled for API trading and in a state Kraken serves.

## Binance.US

**Sources read (2026-10-09):** `docs.binance.us` (first 300,000 of 318,770 characters); support articles on API key creation, supported and unsupported states, API updates and fees; `binance.us/fees`.

**Documented**

- Base `https://api.binance.us`; alternate hosts `api1` and `api2` added on 6 Oct 2026 with identical behavior and limits. Limits are per IP, not per key.
- Keys: Enable Read (default), Enable Spot Trading, Enable Withdrawals. Basic Verification is required to reach API Management, plus two-factor authentication and email confirmation; whether API **trading** needs a higher level is not stated on the page. IP binding is recommended; a key unused for 90 days and not IP-bound is reset to read-only; rotation every 90 days is suggested.
- `POST /api/v3/order`: `newClientOrderId` "A unique ID among open orders. Automatically generated if not sent." and "Orders with the same `newClientOrderID` can be accepted only when the previous one is filled, otherwise the order will be rejected." Maximum length not found. Market buys may use `quoteOrderQty`. `POST /api/v3/order/test` "creates and validates a new order but does not send it into the matching engine".
- `GET /api/v3/order` takes `symbol` and `orderId` or `origClientOrderId`; the 1 Sep 2026 note says that when both are sent they must match or the call returns `-2039`.
- `GET /api/v3/myTrades` returns `id`, `orderId`, `price`, `qty`, `quoteQty`, `commission`, `commissionAsset`, `time`, `isBuyer`, `isMaker`; default limit 500, maximum 1000; the span between `startTime` and `endTime` cannot exceed 24 hours; weight 20 with a symbol, 5 with an `orderId`.
- Errors: no numbered code for a duplicate client ID was found; the messages "Duplicate order sent", "Unknown order sent" and codes `-1118`, `-1119` are listed.
- 2026 changelog: 30 Jul — the WebSocket server pings every 20 seconds (previously every 3 minutes); 1 Sep — `/api/v1` endpoints retired, signatures computed after percent-encoding, `exchangeInfo` weight 10 to 20, `symbolStatus` parameter; 17 Sep — the `listenKey` user-data stream is deprecated, existing connections work until a sunset date still to be announced (targeting Q4 2026), and the replacement is `userDataStream.subscribe.signature` on the WebSocket API with no session logon, events wrapped as `{ subscriptionId, event }`; 6 Oct — alternate hosts.
- Eligibility: the supported-states page (dated 3 Jun 2026) lists unsupported residents of AK, CT, GA, ME, NY, NC, ND, OH, OR, TX, VT, WA and several territories, and "crypto-only" states Kansas and Wisconsin, where USD services are unavailable. The page states the list can change at any time.
- Fees: the fee page's table shows maker 0.0000% for every level, taker 0.0190% for VIP 1–8 and 0.0095% at VIP 9, and BNB/USD (the only "Tier 0" pair) at 0.0000% maker and 0.0095% taker. The prose on the same page says 0.01% and 0.02%; the table appears to be the precise figure. "Use BNB to pay for fees, enjoy 5% off." Tiers update daily at 8:00 PM ET, per the page; the API documentation reportedly states 0:00 UTC, a conflict that was not resolved. No "last updated" date.

**Public observed (`BTCUSD`)**: status `TRADING`, spot only; `PRICE_FILTER` tick 0.01; `LOT_SIZE` minimum 0.00001, step 0.00001; `MIN_NOTIONAL` 1.00; `MARKET_LOT_SIZE` maximum 8.62354612; order types LIMIT, LIMIT_MAKER, MARKET, STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, TAKE_PROFIT_LIMIT. Rate limits declared: `REQUEST_WEIGHT` 6000 per minute, `ORDERS` 100 per 10 seconds and 200000 per day, `RAW_REQUESTS` 300000 per 5 minutes.

**Open items**

1. **2026 API and user-stream changes (card gap).** Documented above. The WebSocket API URL, its stream limits and the executionReport fields were not in the text read: `unverified`. An adapter that polls order and trade endpoints avoids the `listenKey` retirement; one that streams must target the new subscribe method.
2. Whether API trading needs Advanced Verification, and whether the owner's account is in a state with USD services.
3. The maximum `newClientOrderId` length and what a reused ID returns after the first order finished.
4. The balance settlement unit and the trading-fee endpoint to use in a probe.

## OKX US

**Sources read (2026-10-09):** `okx.com/docs-v5/en` (about 300,000 of 409,913 characters, with large parts of the trade section not reaching the reader), the OKX API FAQ for US users (updated 28 Sep 2026), the US notice "OKX Is Consolidating USD and USDC Spot Order Books" (published 8 Sep 2026), the US fee framework page (published 8 Jan 2026).

**Documented**

- Production REST host `openapi.okx.com` does not work for the US. "US and AU users (registered on app.okx.com) should use `us.okx.com` as their API domain." The FAQ repeats that for US users and ties error `50119` to a domain that does not match the account's region. No US WebSocket host is stated in the text read; the global hosts are `wss://ws.okx.com/ws/v5/{public,private,business}`.
- Keys: Read, Trade, Withdraw; up to 20 bound IPs per key; a trade or withdraw key without an IP binding is deleted after 14 days of inactivity; the passphrase cannot be viewed after creation; per the FAQ, accounts need more than 100 USD of assets to create a key, sub-accounts need their own keys, and keys and domains must not be mixed between parent and sub-account.
- Signing: HMAC-SHA256 over timestamp, method, path and body, Base64; REST timestamp within 30 seconds (`50102`); WebSocket login uses a Unix timestamp in seconds.
- Place order: `instId`, `tdMode` (`cash` for spot), `side`, `ordType`, `sz`, `px`, `tgtCcy` (spot market orders only), `clOrdId` of up to 32 alphanumeric characters, "unique among all currently pending (live or partially_filled) orders in the account", reusable once an order is `filled`, `canceled` or `mmp_canceled`. Result fields `ordId`, `clOrdId`, `sCode`, `sMsg`. Rate limit: 60 requests per 2 seconds per user and instrument; cancel the same.
- Demo trading: header `x-simulated-trading: 1`, keys created in the demo environment, demo WebSocket hosts `wspap.okx.com`. The FAQ does not say whether the US domain offers a demo.
- US notice: from 23 Sep to 30 Sep 2026 the USD and USDC spot books ran side by side; on 30 Sep between 03:00 and 04:00 ET the USD books were retired, open USD orders and USD bots were cancelled, funds were unaffected, and USD funds convert automatically to USDC. "Requests must use the corresponding `Crypto-USDC` instrument ID", and to keep trading in USD the request "must explicitly set `tradeQuoteCcy` to `USD`". `USDT-USD` is not affected. The notice does not address order history for the retired USD instrument IDs.
- Fees: the US fee framework (published 8 Jan 2026, effective 1 Feb 2026) groups pairs. Group 1 is BTC, ETH, SOL, XRP, ADA, DOGE, PENGU, PEPE and SUI against USD and USDT; its Regular tier is 0.200% maker and 0.350% taker, falling to 0.100% and 0.200% at VIP 1. The page names only USD and USDT pairs, so **which group the `-USDC` pairs fall in is `unverified`**, and the table predates the September migration. Its VIP 6 and VIP 7 volume ranges overlap.
- Eligibility: the US entity is OKX INC. The FAQ states "US users are expressly prohibited from accessing" foreign OKX products and that certain cryptocurrencies, services and products are unavailable to US users. The OKX API Agreement requires a verified account.

**Public observed:** `us.okx.com/api/v5/public/time` returned `200`. `BTC-USDC` (spot): state `live`, `tickSz` 0.1, `lotSz` 0.00000001, `minSz` 0.0001, `maxMktSz` 1000000, `tradeQuoteCcyList` `["USDG","USD","USDC","RLUSD"]`. `BTC-USD`: code `51001`, "Instrument ID … doesn't exist". This agrees with the notice.

**Open items**

1. **Regional domain and USD→USDC migration (card gap).** Documented and observed above. The adapter must use `us.okx.com`, USDC instrument IDs and an explicit `tradeQuoteCcy`. Account balances may then report USDC instead of USD, which changes the quote asset the ledger and `balance_increments` assume. This is the largest model question for OKX and is `unverified` until an authenticated balance read shows what is reported.
2. The order lookup by client ID, the fills endpoint and the fee-rate endpoint, none of which appeared in the text read.
3. The US WebSocket host and whether demo trading exists for the US domain.
4. Whether the owner's account meets the 100 USD asset rule and holds API-eligible verification.

## Robinhood Crypto

**Sources read (2026-10-09):** `docs.robinhood.com/crypto/trading` (read in full in the built-in browser), the Robinhood Help Center articles "Crypto Trading API" and "Crypto fee tiers". The documentation's own example key pair and signature were not copied.

**Documented**

- Host `https://trading.robinhood.com`. "Available to customers in the United States only", subject to the Robinhood Crypto Customer Agreement. Credentials are created in crypto account settings on web classic; the key's actions are chosen when it is created. Keys issued after 13 Aug 2024 start with `rh-api-`. No IP restriction is documented.
- Authentication: an Ed25519 key pair; the signature covers API key, timestamp, path, method and body; timestamps are valid for 30 seconds.
- Two API versions: v1 places orders **without** fee tiers, v2 places orders **with** fee tiers; read-only actions are on both. v1 has no deprecation timeline. v2 routes orders to partner exchanges and requires an `account_number` on order and holdings calls; v1 prices come from market makers and include a spread.
- Orders: `POST /api/v2/crypto/trading/orders/` takes `symbol`, `client_order_id` (required, a UUID, "for idempotency validation"), `side`, `type` (limit, market, stop_limit, stop_loss) and the matching configuration (`asset_quantity` or `quote_amount`, prices, `time_in_force`). Only USD symbols with `is_api_tradable=true` are accepted. The response carries `id`, `client_order_id`, `state`, `average_price`, `filled_asset_quantity`, an `executions` array, `fee_charged` and `estimated_fee_remaining`.
- Reading orders: the order is read by `id` (`GET .../orders/{id}/`) or listed with filters. The v2 list filters are `account_number`, `symbol`, `side`, `type`, `state`, and created and updated time ranges; the v1 list adds an `id` filter. **No filter by `client_order_id` is documented in either version.** The v2 state filter lists `open`, `canceled`, `filled`, `failed`, `pending`; the v1 filter lists `open`, `canceled`, `partially_filled`, `filled`, `failed`: the two versions name states differently.
- No fills endpoint and no WebSocket are documented. Prices come from authenticated REST calls (`best_bid_ask`, `estimated_price`, up to 10 quantities per call).
- Pagination: `cursor` and `limit`, with `next` and `previous` links.
- Rate limits: 100 requests per minute per account, bursts up to 300, a token bucket; "Rate limits are applied per endpoint and may differ". Errors: `validation_error` (400), `client_error`, `server_error`, status 429 for too many requests.
- Fees: "Fees range from 0.00%–0.95%." The tier table is in a Fee Schedule PDF that was not read. v2 orders count toward 30-day volume and v1 orders do not; the fee rate is set when the order is placed; market and stop orders are always taker; maker/taker fees are "rolling out gradually and may not be available to all customers yet".

**Public observed:** none possible; every endpoint requires a key.

**Open items**

1. **v1 and v2 (card gap).** A v2 integration needs the `account_number` and gets fee tiers; a v1 integration is simpler but has no fee tier and no stated future. Which the owner's credential can use is `unverified`.
2. **Pair tradability (card gap).** Only pairs flagged `is_api_tradable` can be ordered or priced. Which of BTC-USD and ETH-USD are flagged for the owner is `unverified` until a probe reads the trading pairs.
3. Recovery of an order after an ambiguous submission: with no lookup by client ID, an adapter would have to list orders in a created-at window and match on `client_order_id` in each result, if the list returns it. That is `unverified`, and it is the central go/no-go question for this venue.
4. Whether `executions` carry stable, unique IDs the ledger can use as fill IDs.

## Owner-run view-only probe designs

None of these has been run, and no script has been written: a later card writes each under `probes/` using the patterns of the existing probes (hidden key prompt, fixed request budget, result file with steps and counts only). Each probe below names its targets and limits. All four are **read-only**: no order, cancellation, transfer or withdrawal, and no endpoint that needs a trade permission.

**Common rules**

- **Key:** created by the owner on the venue's own site with the narrowest read-only scope, IP-restricted to the owner's address where the venue supports it, deleted after the run.
- **Entry gate:** the probe's first call must confirm the key cannot trade or withdraw, where the venue reports it; if it can, the probe stops before reading anything else.
- **Hosts:** only the named host; a request to any other host is a stop condition.
- **Redaction:** the saved result lists step names, HTTP status, counts and booleans. No key, signature, account number, balance, order ID, client order ID or raw payload is saved or committed. Public market metadata may be recorded.
- **Cleanup:** there is nothing to cancel, because nothing is written. Any WebSocket connection is closed in a `finally` block.
- **Stop conditions (all probes):** any status other than the expected one for a step, any `429`, any response that reports a trade or withdraw permission on the key, a request budget reached, or a clock skew error. After a stop the probe records the reason and exits.
- **Recording:** the owner runs the script; the result is added to this document under **Owner-run authenticated** with the date, commit and the sentence "agent-assisted" or "owner-run".

**K1 — Kraken (host `api.kraken.com`, WebSocket `ws-auth.kraken.com`)**

- Key permissions: Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades only; IP-restricted.
- Requests (budget 12 REST, 1 WebSocket): `GetApiKeyInfo` (entry gate: permissions and restrictions); `Balance`; `TradeVolume` for `XBTUSD` with `fee_schedule` (fee tier and per-pair fees); `OpenOrders` with a cursor; `ClosedOrders` with `limit` 1; `TradesHistory` with `limit` 1 and `with_cursor`; `QueryOrders` twice with an unknown `cl_ord_id` (does it return "not found" or a validation error when `txid` is absent or a dummy); `GetWebSocketsToken`, then one connection that subscribes to `balance` for at most 60 seconds.
- Answers: key scope reporting, fee tier, the balance and increments shape, whether `cl_ord_id` alone is accepted, cursor behavior, private WebSocket reachability.
- Cannot answer from a view-only key: `validate` on `AddOrder`, duplicate `cl_ord_id` behavior and closed-order lookup by client ID, which need a trade-permission key and are decision questions (below).

**B1 — Binance.US (host `api.binance.us`)**

- Key permissions: Enable Read only; IP-bound.
- Requests (budget 8, total weight under 120): `GET /api/v3/account` (check `canTrade` and `permissions`; entry gate); `GET /api/v3/openOrders` for `BTCUSD`; `GET /api/v3/myTrades` for `BTCUSD` with `limit` 1; `GET /api/v3/order` for `BTCUSD` with an unknown `origClientOrderId` twice (the not-found code and message); the documented trading-fee query (endpoint to be fixed from the docs when the probe is written); the API-key restrictions query if the docs list one.
- Answers: scope and eligibility signals, fee schedule for the account, the not-found shape, the order list shape.
- Cannot answer from a read-only key: `POST /api/v3/order/test` (needs trade permission), duplicate-ID behavior, and the WebSocket API (URL not found; a separate public-documentation task).

**O1 — OKX US (host `us.okx.com`)**

- Key permissions: Read only; IP-bound; created with the US account; the passphrase is entered into the hidden prompt.
- Requests (budget 10 REST, 1 WebSocket): `GET /api/v5/account/config` (permission level; entry gate); `GET /api/v5/account/balance` (is the quote asset reported as USD or USDC); `GET /api/v5/account/trade-fee` for `SPOT` and `BTC-USDC` (fee group); `GET /api/v5/trade/order` for `BTC-USDC` with an unknown `clOrdId` twice (not-found code); `GET /api/v5/trade/orders-pending`; `GET /api/v5/trade/fills` with `limit` 1; one private WebSocket login and `account` channel subscription for at most 60 seconds, to learn the US WebSocket host that works.
- Answers: the USD-versus-USDC balance question, fee group of the USDC pairs, whether an order and fills can be read by the documented endpoints, and the working WebSocket host.

**R1 — Robinhood Crypto (host `trading.robinhood.com`)**

- Key: created in crypto account settings with only the read-only actions (accounts, holdings, orders, trading pairs, best bid and ask, estimated price).
- Requests (budget 10, well under 100 per minute): `GET /api/v2/crypto/trading/accounts/` (account number, status, fee tier); `GET /api/v2/crypto/trading/trading_pairs/` for `BTC-USD` and `ETH-USD` (`is_api_tradable`, increments, order-size limits); `GET /api/v2/crypto/marketdata/best_bid_ask/`; `GET /api/v2/crypto/trading/estimated_price/` for two quantities; `GET /api/v2/crypto/trading/holdings/`; `GET /api/v2/crypto/trading/orders/` with a `created_at_start` window and `limit` 1 (the state names, whether `client_order_id` is returned, the pagination links); the same account and pairs calls on `v1` to learn what the credential can use.
- Answers: v1 versus v2 for this account, which pairs are API-tradable, the fee tier, the order list shape and whether a client ID appears in it.

## Go/no-go questions for #14

Each answer below must come from this sheet's evidence class named in brackets, or the item stays `unverified` and is treated as no-go for activation.

1. **Order recovery.** After an ambiguous submission, can the app find the order by its persisted client ID before any retry (`AGENTS.md`)? Binance.US yes in the documentation [D]; Kraken lookup by client ID only since 30 Sep and only documented for open orders [D, O]; OKX unverified [O]; Robinhood no documented lookup [D, O]. Is a venue without a confirmed lookup a no-go, or is a created-at window scan acceptable?
2. **Fills.** Does every venue give per-order fills with stable unique IDs, fees and fee asset the ledger can key on? Kraken and Binance.US yes in the documentation [D]; OKX unverified [O]; Robinhood embeds `executions` without a documented ID [D, O].
3. **Quote asset and balances.** The model reconciles one quote asset. OKX US now trades USDC books with USD funding; do its balances come back as USD or USDC [O]? Kraken, Binance.US and Robinhood are USD [D, P], but Binance.US has no USD service in Kansas and Wisconsin [D].
4. **Settlement rounding.** The per-fill settlement rule from #133 depends on each venue's balance unit (`balance_increments`). None is known for these four [O]. Is observing it on the owner's first read-only balance probe plus a tiny-value activation an accepted way to establish it?
5. **Streaming and polling.** The app reconciles by polling every five minutes. Is polling sufficient for every venue? Robinhood has no stream [D]; Binance.US's `listenKey` stream is being retired and its replacement's limits are unverified [D]; Kraken's private channels are documented [D].
6. **Test fidelity.** No venue offers a self-service spot sandbox (Kraken `validate` and an on-request UAT; Binance.US test order; OKX demo of unknown US availability; Robinhood none) [D]. Since CI must not place real orders, is a tiny-value live order, owner-run, the accepted way to prove an adapter, and who sets its size and limit?
7. **Key scope and IP binding.** Robinhood documents no IP restriction [D]; OKX deletes unbound trade keys after 14 days of inactivity and Binance.US resets inactive unbound keys to read-only [D]. Are those lifecycle rules acceptable for an unattended service, and is the paper VPS address the one to bind?
8. **Eligibility.** For each venue, is the owner's account in a served state with the verification level API trading needs [O]? The published state lists and verification statements are in the venue sections.
9. **Fees against the strategy.** The strategy's expected edge must clear each venue's taker fee. The figures that were read differ by more than an order of magnitude (Binance.US about 0.02%, OKX US 0.35% at the lowest tier, Kraken Pro unconfirmed) [D]. Which venue's fee model should the backtest use first, and does Robinhood's spread-inclusive pricing and v2 fee tier change that?
10. **Venue-specific volatility of the rules.** Four rules changed in the last six weeks (Kraken 30 Sep, Binance.US 17 Sep, OKX 30 Sep, Binance.US 1 Sep). Is a re-read of the documentation, dated in the pull request, required immediately before each adapter card starts?

## Sources read on 2026-10-09

Kraken: `docs.kraken.com/api/docs/rest-api/add-order/`, `.../get-orders-info/`, `.../get-trade-history/`, `.../get-tradable-asset-pairs/`, `.../guides/spot-rest-ratelimits/`, `.../guides/spot-ws-intro/`, `.../change-log`, `docs.kraken.com/home/guides/quickstart`, `kraken.com/features/fee-schedule`, `support.kraken.com/articles/quick-start-for-clients-in-the-united-states`, and the support articles on creating an API key and the nonce window.

Binance.US: `docs.binance.us/`, `support.binance.us/en/articles/9842800-how-to-create-an-api-key-on-binance-us`, `.../9842798-list-of-supported-and-unsupported-states-and-regions`, `.../9843375-explore-new-api-updates`, `binance.us/fees`.

OKX US: `okx.com/docs-v5/en/`, `okx.com/en-us/help/api-faq`, `okx.com/en-us/help/okx-is-consolidating-usd-and-usdc-spot-order-books-effective-september-30-2026`, `okx.com/en-us/help/updates-to-us-fee-framework-2026`.

Robinhood: `docs.robinhood.com/crypto/trading/`, `robinhood.com/us/en/support/articles/crypto-api/`, `robinhood.com/us/en/support/articles/crypto-fee-tiers/`.

Observation log (public, unauthenticated, 2026-10-09 about 13:25 UTC, one request each, all `200`): `api.kraken.com/0/public/Time`, `.../SystemStatus`, `.../AssetPairs?pair=XBTUSD,ETHUSD`; `api.binance.us/api/v3/ping`, `.../time`, `.../exchangeInfo?symbol=BTCUSD`; `us.okx.com/api/v5/public/time`, `.../public/instruments?instType=SPOT&instId=BTC-USDC`, and the same for `BTC-USD`.

## Validation boundary

This sheet is documentation and public observation only. It is not evidence that any of the four venues will accept the app's orders, and it does not satisfy any owner-run criterion. Owner-run authenticated results, when they exist, are added above as a separate class with their date and commit. Documentation changes often; every finding here carries the date it was read and must be rechecked before an adapter card relies on it.
