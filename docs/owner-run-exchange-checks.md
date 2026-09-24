# Owner-run exchange checks

Three checks confirm that the trading code works against the real exchanges, not
only against the stand-ins the automated tests use. Each is a one-command script
in `probes/`. Your part is creating a key on the exchange's website and typing it
into a hidden prompt. Claude records each result in this file afterwards.

| Check | What it proves | Who runs it | Status |
| --- | --- | --- | --- |
| 1. Gemini Sandbox lifecycle and trading loop | Orders placed through our Gemini code behave as expected on Gemini's test exchange, and one pass of the trading loop completes there | Owner | Tried 2026-09-24; findings fixed in #33; to run again |
| 2. Coinbase read-only reconciliation | Our Coinbase code reads your real account correctly and reconciles cleanly | Owner | Not yet run |
| 3. Coinbase sandbox capture | Our Coinbase code reads Coinbase's real response formats | Claude, with the owner's approval | Done 2026-09-24 |

## What the scripts can and cannot do

- **Gemini:** runs only against the Sandbox, Gemini's play-money test exchange. The code refuses any other address.
- **Coinbase:** the check uses a View-only key and stops before reading anything unless Coinbase confirms the key can neither trade nor transfer. It never places an order.
- **Limits:** each script may contact only its named exchange address and make a fixed number of requests. It cancels any Sandbox order still open at the end, even if something fails partway.
- **Keys:** they are typed into a hidden prompt or read from a file you choose. They are never shown, saved, or written into a result, and never paste a key into a chat.
- **Results:** each run saves a result file in `C:\Users\garni\crypto-trader-probe-results\`. The file lists steps, statuses, and counts only: no keys, account numbers, order numbers, or balances.

## Before either check

Open a terminal in the project folder, `C:\Users\garni\Documents\algorithmic-crypto-trader`. Make sure it has the latest code: run `git pull`, or ask Claude to update it.

## Check 1: Gemini Sandbox

1. Sign in to the **Gemini Sandbox** website with the Sandbox account from Phase 0. It is separate from any real Gemini account.
2. Check that the Sandbox **Primary** account holds some US dollars; the run needs roughly 1% of one bitcoin's price, about $700 of test money. If it holds none, add test funds on the Sandbox website. The script's first step checks this and stops before placing anything if there is not enough.
3. Under **Settings → API**, create a new API key for the **Primary** account with the **Trader** role only.
   - Create it for the Primary account itself, not at the account-group ("Master") level: Gemini refuses a group-level key's requests unless each names an account.
   - Do not tick Fund Manager or Administrator.
   - Leave **Require Heartbeat** off.
   - Keep the page open; the secret is shown once.
4. In the terminal, run:

   ```
   python -m probes.gemini_sandbox_lifecycle
   ```

5. Paste the API key when asked, press Enter, then do the same for the secret. Nothing appears on screen as you paste; that is expected.
6. The run takes about a minute. It ends with **PASS** or **NEEDS REVIEW**, a line for each step, and where it saved the result. Tell Claude it finished.
7. Afterwards you may delete the Sandbox key on the Gemini Sandbox website.

What the steps check (any refusal also names Gemini's own reason code, such as `InvalidSignature` or `InsufficientFunds`):

| Step | Meaning |
| --- | --- |
| authenticate | The key works against the Sandbox. |
| usd_available | The account holds enough test dollars for every order in the run. Only whether it is enough is recorded, never the balance. |
| capped_market_order | How the trading loop buys on Gemini: an order that may pay at most 1% over the current price, and whatever cannot fill within that cap is cancelled at once. "Refused" means little was offered near the Sandbox's quoted price. |
| resting_limit, recovery_by_client_order_id, cancel | An order priced far below the market rests; a freshly started adapter finds it by our own order ID, as after a restart; then it is cancelled. |
| undersized_rejection | A too-small order is refused. |
| partial_fill | Buys slightly more than the best offer holds, so part fills. Skipped if the Sandbox's book is too deep for the script's small size cap. |
| trading_loop | One full pass of the trading loop: strategy, risk checks, and the order. "Refused" names the risk check that stopped it. |
| cleanup | Any order still open was cancelled. If this fails, cancel the listed orders on the Sandbox website. |

## Check 2: Coinbase read-only

1. Sign in to the **Coinbase Developer Platform** and create a new API key:
   - **Permissions:** View only. Leave Trade and Transfer off.
   - **Signature algorithm:** ECDSA, under the advanced settings. Our code cannot use Ed25519, the default for new keys. If ECDSA is not offered, stop and tell Claude; that answers an open question.
   - Download the key file when it is offered.
2. In the terminal, run the command below, replacing the path with where the key file was saved (you can also leave the path off and paste it when asked):

   ```
   python -m probes.coinbase_readonly_reconcile "C:\Users\garni\Downloads\cdp_api_key.json"
   ```

3. It ends with one of:
   - **PASS:** the account was read twice and reconciled with no differences.
   - **REFUSED:** the key can trade or transfer. Delete it and create a View-only key.
   - **Stopped: this is an Ed25519 key:** create the key again with ECDSA.
   - **FAILED** with a 401 note: Coinbase rejected the key. Tell Claude; that also answers the open question on key types.
   - **DIVERGED:** the account changed between the two reads, for example because something traded. Run it again.
4. Tell Claude it finished. Then delete the downloaded key file, or move it into a password manager. Revoke the key on Coinbase if it is not needed again.

## Results

| Date | Check | Run by | Commit | Result |
| --- | --- | --- | --- | --- |
| 2026-09-24 | 3. Coinbase sandbox capture | Claude (agent-run; the owner approved a one-time exception to the owner-run rule because it needs no key or account) | branch `feat/31-owner-run-exchange-probes` | **PASS**: 11 responses captured with 11 of 15 allowed requests, all replayed through the adapter successfully |
| 2026-09-24 22:12 UTC | 1. Gemini Sandbox | Owner | `2148b26` | **Stopped at the first request (HTTP 400).** No order was placed. The key was replaced with one created for the Primary account. |
| 2026-09-24 22:19 UTC | 1. Gemini Sandbox | Owner | `2148b26` | **NEEDS REVIEW.** The key authenticated (6 currencies). A market buy by coin quantity was refused (`MissingTotalSpend`), and a limit buy was refused for insufficient funds (HTTP 406). No order was placed or left open; 4 requests. |

What the Gemini attempts showed, and what #33 changed:

- **Market orders:** Gemini's market buy takes a dollar amount, not a coin quantity, and has no price protection. The owner chose (2026-09-24) to send market requests as immediate-or-cancel limit orders capped 1% past the current price instead.
- **Insufficient funds:** that refusal (HTTP 406) was not recognised as a rejection, so in the trading loop it would have halted trading after five minutes instead of being recorded as rejected. It is now a clean rejection.
- **The script:** it now names Gemini's reason code for any refusal, and checks the dollar balance before placing anything.

What the Coinbase sandbox capture showed:

- **Response formats:** accounts, order details, fills, cancel results, and edit results all have the shapes the adapter reads, including the nested `order_configuration` that restart recovery relies on. See `tests/test_coinbase_sandbox_capture.py`.
- **Rejection reasons:** a rejected order's reason was being recorded as a raw data dump. It now reads as `INSUFFICIENT_FUND: Insufficient balance in source account`.
- **Sandbox limits:**
  - Get Order answers only for the sandbox's own sample orders, not for the ID its Create Order returns.
  - Create Order echoes a fixed client order ID rather than ours.
  - The sample orders use client order IDs that are not UUIDs.

  These are properties of the static sandbox, not of production. The tests account for them.
