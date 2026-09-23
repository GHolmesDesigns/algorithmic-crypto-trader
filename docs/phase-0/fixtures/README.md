# Phase 0 fixtures

The `gemini-sandbox-*.json` files contain public, non-secret Gemini Sandbox market-data
observations from 2026-09-22. `owner-run-2026-09-22.json` contains the redacted
account-specific verification from that date, including Coinbase read-only checks and
Gemini Sandbox order checks. It deliberately records shapes, statuses, constraints,
and boolean lifecycle outcomes rather than account identifiers, balances, or order
identifiers.

Do not add raw authenticated responses here. Replace account identifiers, API keys,
signatures, JWTs, authorization headers, client order IDs, and identifying balances
before adding an owner-run fixture.
