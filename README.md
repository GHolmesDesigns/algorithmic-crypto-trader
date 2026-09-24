# algorithmic-crypto-trader

Personal, safety-first algorithmic crypto-trading platform with Coinbase live-trading integration, Gemini Sandbox rehearsal, and deterministic paper/replay modes.

> **Safety notice:** This project is experimental. It is not financial advice, and no production credentials, account data, or live-trading activity belongs in this public repository.

## Current status

The project has completed Phase 0 validation and is implementing the Phase 1 foundation. Live trading is not enabled by this repository.

Agents and contributors working concurrently should read [AGENTS.md](AGENTS.md) before claiming a card or creating a branch.

The intended execution modes are:

- `backtest` — historical data with explicit cost and slippage assumptions.
- `replay` — recorded streams through the real strategy, risk, and execution paths.
- `paper` — live market data against a simulator or Gemini Sandbox.
- `live` — a separately guarded Coinbase production path, only after all safety gates pass.

## Development policy

- Never commit API keys, private keys, credentials, production account identifiers, database dumps, or live operational logs.
- Use local fixtures and simulated brokers for automated tests.
- Keep expensive integration, replay, soak, and live-provider checks manual or scheduled rather than running them on every change.
- The default GitHub workflow runs repository and Python quality gates. It does not place orders or contact a live exchange.

## Foundation quick start

Python 3.12+ is required. Install the project and development checks, then start the service:

```text
python -m pip install -e ".[dev]"
python -m app
```

The service defaults to `TRADING_MODE=backtest`, refuses unsafe credential/mode combinations before initialization,
and exposes `GET /health`. PostgreSQL is the application database; SQLite is reserved for isolated tests and
portable backtest/replay archives. To run the application and PostgreSQL together, use `docker compose up --build`.

See [docs/foundation.md](docs/foundation.md) for package boundaries and the safety contracts established in Phase 1.1.
The [user manual](docs/user-manual.md) provides a non-technical operator guide and an engineering/AI-agent reference.
The authenticated operator surface contract is documented in
[docs/phase-1.7-operator-surface.md](docs/phase-1.7-operator-surface.md). The Phase 1 acceptance
evidence, including what still needs owner-run provider verification, is in
[docs/phase-1-gate-acceptance.md](docs/phase-1-gate-acceptance.md).

## License

No license has been selected yet. Until one is added, public visibility does not grant permission to reuse the code.
