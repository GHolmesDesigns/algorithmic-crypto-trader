# Foundation package

Issue #3 establishes the contracts that later packages must use:

- `core.models` contains frozen Pydantic models and uses `Decimal` for every monetary quantity.
- `core.guards` validates the literal `backtest`, `replay`, `paper`, and `live` modes before app initialization. A live boot requires both `CREDENTIAL_SCOPE=trade` and `LIVE_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING`; trade-capable credentials are refused in every non-live mode.
- `core.logging` emits JSON records, carries a correlation ID, and recursively redacts secret-bearing fields before output.
- `core.resilience` supplies a token bucket and circuit breaker. They are provider-neutral and must be instantiated for all modes, including backtest and replay.
- `brokers.interface.BrokerInterface` accepts an approved request and exposes an explicit `BrokerCapabilities` object. `SimulatedBroker` is the deterministic contract-test implementation.
- `execution.submit_approved_order` makes the risk boundary visible in code: a raw `Signal` cannot be submitted.
- `db` and `alembic` define the PostgreSQL audit spine. SQLite is allowed only for isolated tests and portable backtest/replay archives.

Run the service locally with one command after installing the project:

```text
python -m app
```

The default `backtest` mode prints a loud startup banner and starts the health endpoint at
`http://127.0.0.1:8000/health`. Docker Compose starts the application with PostgreSQL:

```text
docker compose up --build
```

No command in this foundation package contacts an exchange or submits an order. Provider access,
database migrations, and live activation remain explicit later-stage operations.
