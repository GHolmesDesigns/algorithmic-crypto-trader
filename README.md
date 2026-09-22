# algorithmic-crypto-trader

Personal, safety-first algorithmic crypto-trading platform with Coinbase live-trading integration, Gemini Sandbox rehearsal, and deterministic paper/replay modes.

> **Safety notice:** This project is experimental. It is not financial advice, and no production credentials, account data, or live-trading activity belongs in this public repository.

## Current status

The project is in planning and Phase 0 validation. Live trading is not enabled by this repository.

The intended execution modes are:

- `backtest` — historical data with explicit cost and slippage assumptions.
- `replay` — recorded streams through the real strategy, risk, and execution paths.
- `paper` — live market data against a simulator or Gemini Sandbox.
- `live` — a separately guarded Coinbase production path, only after all safety gates pass.

## Development policy

- Never commit API keys, private keys, credentials, production account identifiers, database dumps, or live operational logs.
- Use local fixtures and simulated brokers for automated tests.
- Keep expensive integration, replay, soak, and live-provider checks manual or scheduled rather than running them on every change.
- The default GitHub workflow is intentionally a lightweight repository preflight. It does not place orders or contact a live exchange.

## License

No license has been selected yet. Until one is added, public visibility does not grant permission to reuse the code.
