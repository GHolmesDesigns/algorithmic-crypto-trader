# Algorithmic Crypto Trader user manual

- **Manual version:** Draft 1
- **Applies to:** application version `0.1.0` at repository commit `05721e8`
- **Last reviewed:** 2026-09-24

> **Safety notice**
>
> This project is experimental software, not financial advice. The current
> repository does not authorize live trading. Never paste exchange credentials,
> operator tokens, account details, database exports, or unredacted operational
> logs into this repository, an issue, a pull request, a chat, or an AI prompt.

## Choose your path

- If you operate the service and do not need to change code, start with
  [Part I: Operator guide](#part-i-operator-guide).
- If you are an AI agent, Python contributor, or .NET engineer integrating with
  the service, start with [Part II: Engineering and agent guide](#part-ii-engineering-and-agent-guide).
- If the service is in an unsafe or uncertain state, go directly to
  [Incident response](#incident-response).

## What the current application does

The service provides a safety-first foundation for research, simulated trading,
Gemini Sandbox rehearsal, and a separately guarded Coinbase live path. On a
normal service start it:

1. validates the selected mode and credential scope;
2. loads the persistent kill switch;
3. constructs only the broker allowed for that mode;
4. recovers pending or unknown orders by their persisted client order ID;
5. reconciles persisted state with the broker, when a broker is configured;
6. starts scheduled reconciliation when a broker is configured; and
7. exposes health checks and an authenticated operator dashboard.

The broker is authoritative during reconciliation. If the database and broker
disagree, or if required state cannot be read, the system fails closed and
halts trading.

### Current release boundary

This draft describes the software as it exists today, not the final planned
product:

- `app.main` does **not yet start the live market-data ingestor or the automated
  `TradingCycle`**. A running web service is therefore not proof that a strategy
  is receiving market data or placing simulated orders.
- The operator surface can show P/L as unavailable and the primary strategy
  heartbeat as unknown until those producers are wired into the runtime.
- Phone-push and email alerts are delivered only when alert sinks are explicitly
  injected. The default application entry point does not configure those sinks.
- Real Gemini adapter lifecycle verification, Coinbase read-only reconciliation,
  the 72-hour market-data run, and the seven-day unattended reconciliation soak
  still require the documented owner-run evidence.
- Coinbase order placement remains a later, explicitly authorized phase. Do not
  interpret the presence of the adapter or live-mode guards as permission to use
  them.

# Part I: Operator guide

## Your responsibilities

As an operator, you monitor the system, pause or stop it when something is
uncertain, and re-arm it only after the cause is understood. You do not need to
read code or use a command line for routine dashboard operation.

You should receive the following through a private channel from the system
owner or administrator:

- the operator dashboard address;
- either an operator token or an administrator token;
- the expected trading mode; and
- the person or team to contact during an incident.

Treat the token like a password. Do not store it in browser bookmarks, URLs,
screenshots, tickets, or chat messages.

## Key terms

| Term | Plain-language meaning |
| --- | --- |
| Trading mode | The environment in which the system is allowed to operate. |
| Broker | The simulated or external venue that holds balances, orders, and positions. |
| Kill switch | The persistent safety control that determines whether new trading work may proceed. |
| Startup recovery | The check performed before the service accepts requests. It resolves uncertain orders and compares saved state with the broker. |
| Reconciliation | A repeated comparison between local records and the broker's records. |
| Client order ID | The system's unique, persisted identifier for an order. It is used to find an uncertain order without submitting a duplicate. |
| Fail closed | Stop or refuse activity when required information is missing, stale, or uncertain. |

## Trading modes

| Mode | Purpose | External orders? |
| --- | --- | --- |
| `backtest` | Run a strategy over historical data. | No |
| `replay` | Feed recorded market data through the normal strategy, risk, and execution path. | No |
| `paper` | Rehearse using a simulator or Gemini Sandbox. | Never a production Coinbase order |
| `live` | Separately guarded Coinbase production path. | Potentially, but not authorized by this manual or the current project phase |

If the dashboard shows a mode other than the one you were told to expect,
select **PAUSE** and contact the administrator. If it unexpectedly shows
`live`, select **EMERGENCY STOP**.

## Kill-switch states

| State | Meaning | Operator action |
| --- | --- | --- |
| `running` | The safety control permits work, subject to every other risk gate. It does not prove that a strategy is active. | Continue monitoring. |
| `paused` | New trading work should not proceed. Use this for planned investigation or uncertainty that is not yet an emergency. | Investigate; do not re-arm until the cause is understood. |
| `halted` | The system or an operator identified a critical condition. Automatic processes cannot re-arm it. | Escalate and keep it halted until the re-arm checklist is complete. |

The kill-switch state is stored across application and host restarts when the
deployment uses the configured persistent volume.

## Sign in

1. Open the private service address supplied by the administrator and add
   `/operator/login` if it is not already present.
2. Confirm that the browser shows the expected secure site. In production, do
   not continue through a certificate warning.
3. Enter the token you received through the approved private channel.
4. Select **Sign in**.

The browser receives a protected session cookie that lasts up to eight hours.
Refresh the current standalone dashboard page manually: the template declares
a 15-second HTMX refresh, but the current page does not load the HTMX library.
There is currently no sign-out button, so close the browser session on a shared
device. Never use the `?token=` query parameter even though the server accepts
it for compatibility: URLs are often recorded in history and logs.

## Read the dashboard

Review these items from top to bottom:

| Dashboard item | Healthy or expected | Stop and investigate when |
| --- | --- | --- |
| Application | `healthy` when broker refresh succeeds; liveness is separately available at `/health`. | The page cannot load or the service repeatedly restarts. |
| Trading mode | Matches the owner-approved mode. | It differs from the approved mode or unexpectedly says `live`. |
| Strategy version | Matches the reviewed release. `unknown` is possible until runtime wiring is complete. | It changed unexpectedly or an active strategy reports an unknown version. |
| Broker connectivity | `healthy — broker state refreshed`, or `not_configured` for an intentionally credential-free deployment. The JSON state includes the check time, but the current HTML page does not render it. | `unavailable`, especially if positions or orders could exist. |
| Risk state | The expected kill-switch state. | It changes without an understood operator action or incident. |
| Startup recovery | `reconciled`, or `no_broker` when intentionally running without a broker and with no pending orders. | `halted`, or the detail mentions a pending order, missing baseline, unavailable broker, or divergence. |
| Portfolio | `current — broker-authoritative snapshot` when a broker is configured. | `unavailable` when broker state is required. Last-known values are diagnostic only. |
| P/L | May be `unavailable` in the current release. | Do not infer profitability or safety from a missing value. |
| Balances and positions | Plausible and consistent with the approved environment. | A value is unexpected, duplicated, missing, or clearly belongs to another environment. |
| Orders, fills, and signals | Counts and recent activity match expectations. | An order is unknown, pending unexpectedly, duplicated, or lacks an expected fill. |
| Strategies | Expected strategy reports a recent healthy heartbeat. | Heartbeat is absent, stale, unhealthy, or unexpectedly changes version. The current runtime may show `unknown`; treat that as not proven active. |
| Errors and alerts | Empty, or a previously reviewed condition. | Any new critical alert, broker error, reconciliation divergence, or repeated error. |

`/health` proves only that the web process responds. It does not prove that the
broker, strategy, market data, database, reconciliation, or trading path is
healthy.

## Routine operating check

At the beginning of a monitoring period:

1. Sign in and confirm the expected trading mode.
2. Confirm the kill-switch state.
3. Read the full startup-recovery status and detail.
4. Confirm broker connectivity. Ask an engineer to verify the authenticated
   JSON refresh time when that evidence is required; the current HTML page does
   not display it.
5. Confirm balances and positions are plausible for this environment.
6. Review orders, fills, signals, strategy heartbeats, alerts, and errors.
7. When a broker is configured, ask an engineer or approved monitoring tool to
   confirm the `reconciliation` object in authenticated `/operator/state`; the
   current HTML page does not render it.
8. Record only the approved, redacted result in the private operations log.

Do not mark the system healthy solely because `/health` returns a success.

## Controls

### Pause

Use **PAUSE** when you need time to investigate, when expected data is missing,
or before a planned maintenance action. Both operator and administrator tokens
can pause.

### Emergency stop

Use **EMERGENCY STOP** immediately when:

- the mode or credentials appear wrong;
- an order may be duplicated or its result is uncertain;
- balances or positions do not match the broker;
- the database, broker, market data, or risk inputs cannot be trusted;
- the service behaves unexpectedly after a restart; or
- you are unsure whether continued operation is safe.

Both operator and administrator tokens can stop the system. A halted state
cannot be cleared automatically.

### Re-arm

Only an administrator can see and use **RE-ARM** when a separate administrator
token is configured. Re-arming changes the kill switch to `running`; it does
not repair a broker, database, data feed, strategy, or unresolved order.

Complete every item before re-arming:

1. Identify and document the original cause.
2. Confirm the approved mode and credential scope.
3. Resolve every pending or unknown order by looking it up at the broker with
   its persisted client order ID. Never submit it again merely because the first
   response was uncertain.
4. Confirm the broker-authoritative balances, positions, orders, and fills.
5. Confirm startup recovery and reconciliation are clean.
6. Confirm required market data and risk inputs are current.
7. Obtain the incident owner's approval when the operating procedure requires it.
8. Select **RE-ARM**, then confirm the dashboard reports `running` and no new
   error or divergence appears.

If any item is unknown, leave the system paused or halted.

## Incident response

### Broker is unavailable

1. Select **PAUSE**. Use **EMERGENCY STOP** if an order could be in flight.
2. Treat displayed balances and positions as last-known diagnostic data, not
   current truth.
3. Do not retry an order.
4. Ask an engineer to verify network access, provider status, and the exact
   client order IDs of any uncertain orders.

### Startup recovery is halted

1. Leave the system halted.
2. Record the recovery status and redacted detail.
3. Ask an engineer to inspect the persisted orders, portfolio baseline, and
   broker state.
4. Re-arm only after the broker and local state reconcile cleanly.

### Reconciliation reports a divergence

1. Leave the system halted; the broker's record is authoritative.
2. Record the number and type of differences without copying credentials,
   account identifiers, or raw provider payloads.
3. Have an engineer review the discrepancy event and the adopted broker state.
4. Do not edit the database to make the alert disappear.

### An order is pending or unknown

1. Select **EMERGENCY STOP** if the system is not already halted.
2. Find the persisted client order ID in the authorized audit tooling.
3. Query the broker for that same ID.
4. Update local state through the normal recovery path.
5. Never create a replacement order until the original is proven absent and the
   approved recovery procedure permits a new submission.

### The dashboard is unavailable

1. Ask an authorized engineer to check the liveness endpoint at `/health`.
2. If liveness also fails, keep the independent environment/file kill-switch
   actuation at `paused` or `halted` and escalate.
3. Do not restart repeatedly. A restart can trigger recovery and must be
   reviewed afterward.

### Information to collect safely

Provide the engineer with:

- UTC date and time;
- expected mode and displayed mode;
- kill-switch, recovery, connectivity, and reconciliation statuses;
- the visible, redacted error or alert text;
- the reviewed application commit or release identifier; and
- the steps immediately before the problem.

Do not provide tokens, keys, private URLs, hostnames, IP addresses, account
identifiers, database dumps, or unredacted provider responses in public records.

# Part II: Engineering and agent guide

## Engineering orientation

The service is implemented in Python 3.12 with FastAPI, SQLAlchemy, Alembic,
PostgreSQL, Pydantic, and provider-neutral broker contracts. A .NET component
integrates over authenticated HTTP/JSON; it does not need to embed Python or
reimplement trading rules.

An AI agent must read `AGENTS.md` before claiming or editing a card. The guide
requires repository inspection, an uncontested claim, an isolated worktree,
exact-path staging, fail-closed behavior, and separate reporting for local
validation, exact-head remote CI, and owner-run provider evidence.

## Repository map

| Path | Responsibility |
| --- | --- |
| `core/` | Shared models, modes, identifiers, startup guards, logging, and resilience. |
| `brokers/` | Provider-neutral interface, simulator, Gemini Sandbox, and Coinbase adapters. |
| `data/` | Coinbase public market data, storage, normalization, validation, replay recording, and gap fill. |
| `strategy/` | Pure strategy and research code. It must not import brokers, databases, HTTP, or network code. |
| `risk/` | Ordered, fail-closed risk gates and the persistent kill switch. |
| `execution/` | Audit persistence, idempotent submission, ambiguity resolution, and fills. |
| `portfolio/` | Broker-authoritative state, reconciliation, snapshots, and scheduling. |
| `api/` | Authenticated operator views, controls, health, and alert routing. |
| `app/` | Composition root, startup recovery, broker selection, trading loop, and replay runner. |
| `db/`, `alembic/` | SQLAlchemy persistence and schema migrations. |
| `deploy/` | Docker/VPS startup, drills, encrypted backup, and restore verification. |
| `tests/` | Unit, contract, integration, failure-path, and controlled process-kill evidence. |

Provider-specific behavior stays in `brokers/`. Do not bend strategy, risk,
portfolio, or reconciliation contracts around one venue.

## Core safety invariants

- Use `Decimal` for prices, quantities, fees, balances, and P/L.
- Keep mode values exactly `backtest`, `replay`, `paper`, and `live`.
- Strategies return signals and cannot submit orders or access external systems.
- Execution accepts an approved `RiskApproval`, not a raw signal.
- Persist the unique `client_order_id` before calling a broker.
- Resolve ambiguous submissions by client order ID before any retry.
- Missing or stale risk input refuses the order.
- The broker is authoritative during reconciliation.
- Divergence alerts the operator and trips the configured safety response before
  new entries.
- `HALTED` persists and requires authenticated manual re-arm.
- Automated tests never contact an exchange or place a real order.
- Credentials may never allow withdrawals or transfers.

## Mode, broker, and credential matrix

| Mode | `CREDENTIAL_SCOPE` | `BROKER_PROVIDER` | Result |
| --- | --- | --- | --- |
| `backtest` | `none` or `view` | empty | Valid; no provider broker. |
| `replay` | `none` or `view` | empty | Valid; no provider broker. |
| `paper` | `none` | empty | Valid credential-free service. `SimulatedBroker` requires deliberate test or application composition; the default entry point does not construct it. |
| `paper` | `view` | `gemini-sandbox` | Valid with Gemini Sandbox credentials under the current startup contract. |
| `paper` | any | `coinbase` | Refused. Paper orders must not reconcile against a Coinbase production account. |
| `live` | `trade` | `coinbase` | Structurally allowed only with exact confirmation and a Coinbase key proven able to trade and unable to transfer. Still not authorized by the current project phase. |

The effective code currently rejects `CREDENTIAL_SCOPE=trade` in all non-live
modes. If Gemini Sandbox needs order-capable credentials, preserve the public
configuration label required by the startup contract and treat provider scope
separately; do not weaken the guard without an explicit product and safety
decision.

## Local setup

### Prerequisites

- Python 3.12 or newer;
- Git;
- Docker with Docker Compose for the application database path; and
- PowerShell on Windows for the examples below.

### Install for development

From an isolated worktree:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Create a local `.env` only when the approved workflow requires it, protect it
with local secret-management controls, and never commit it. Git ignoring the
file does not make its contents safe.

### Safest service start

The Compose path starts PostgreSQL, runs Alembic migrations in the app
entrypoint, persists database and kill-switch volumes, and defaults to
`TRADING_MODE=backtest`, `CREDENTIAL_SCOPE=none`, and no broker:

```powershell
docker compose up --build
```

Check liveness without exposing state:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Stop containers without deleting state:

```powershell
docker compose stop
```

Never use `docker compose down -v` against an environment whose database or
kill-switch volumes matter.

### Direct Python start

`python -m app` starts the API on port 8000 after loading startup settings and
running startup recovery. It expects a reachable database whose migrations are
current. Prefer Compose unless you deliberately manage PostgreSQL and Alembic
yourself.

## Configuration reference

| Variable | Default | Contract |
| --- | --- | --- |
| `TRADING_MODE` | `backtest` | One of `backtest`, `replay`, `paper`, or `live`. |
| `CREDENTIAL_SCOPE` | `none` | One of `none`, `view`, or `trade`. `trade` is refused outside `live`. |
| `LIVE_CONFIRMATION` | empty | Must equal `I_UNDERSTAND_LIVE_TRADING` in `live`; necessary but not sufficient authorization. |
| `DATABASE_URL` | local PostgreSQL URL | SQLAlchemy URL. Production uses PostgreSQL; SQLite is limited to tests and portable backtest/replay archives. |
| `LOG_LEVEL` | `INFO` | Application log level. Secret-bearing fields are redacted by the logging layer. |
| `BROKER_PROVIDER` | empty | Empty, `gemini-sandbox`, or `coinbase`, subject to the mode matrix. |
| `GEMINI_API_KEY` | empty | Required with `BROKER_PROVIDER=gemini-sandbox`. Secret. |
| `GEMINI_API_SECRET` | empty | Required with `BROKER_PROVIDER=gemini-sandbox`. Secret. |
| `COINBASE_API_KEY` | empty | Required with `BROKER_PROVIDER=coinbase`. Secret. |
| `COINBASE_PRIVATE_KEY` | empty | Required with `BROKER_PROVIDER=coinbase`. Secret; may contain line breaks. |
| `OPERATOR_TOKEN` | empty | Required for authenticated operator access. Also signs session cookies. |
| `OPERATOR_ADMIN_TOKEN` | empty | Optional locally, required by the VPS Compose override. When present, only this token may re-arm; when absent, the operator token receives administrator role. |
| `OPERATOR_COOKIE_SECURE` | `0` | Set to `1` behind production HTTPS so the browser sends the session cookie only over secure transport. |
| `KILL_SWITCH_FILE` | unset | Persistent JSON state path; Compose uses `/var/lib/trader/kill-switch.json`. |
| `TRADING_KILL_SWITCH` | empty | Independent external request for `paused` or `halted`. `running` never re-arms. Invalid content halts. |
| `STRATEGY_VERSION` | `unknown` | Version displayed on the operator surface. |
| `RECONCILE_INTERVAL_SECONDS` | `300` | Broker reconciliation interval; must be greater than 0 and no more than 3600. |

Startup must fail rather than silently correcting an invalid mode, scope,
provider, credential, live confirmation, or reconciliation interval.

## HTTP interface

| Method and path | Authentication | Purpose |
| --- | --- | --- |
| `GET /health` | None | Liveness only; returns `{"status":"ok"}`. |
| `GET /health/detail` | Operator | Application, broker, and strategy health. |
| `GET /health/strategies` | Operator | Strategy heartbeat list. |
| `GET /operator/login` | None | Browser login form. |
| `POST /operator/login` | Token in form body | Establishes an HttpOnly, SameSite=Strict session lasting up to eight hours. |
| `GET /operator` | Operator | Server-rendered dashboard. |
| `GET /operator/fragment` | Operator | Dashboard fragment used for the 15-second refresh. |
| `GET /operator/state` | Operator | Full operator snapshot as JSON. |
| `GET /operator/kill-switch` | Operator | Current kill-switch state. |
| `POST /operator/pause` | Operator | Persist `paused`. |
| `POST /operator/emergency-stop` | Operator | Persist `halted`. |
| `POST /operator/rearm` | Administrator | Persist `running` after manual review. |

For programmatic access, send the token in the `x-operator-token` header. Do
not put it in a URL. Cookie authentication is intended for the browser. The
current API does not implement a general trading-command endpoint.

FastAPI also exposes its generated API documentation by default. A production
reverse proxy should apply the deployment's access policy to `/docs`,
`/redoc`, and `/openapi.json`; their existence is not a substitute for the
maintained safety contracts in this manual and the repository documentation.

## .NET read-only integration example

Use a named or typed `HttpClient`, keep the token in an approved secret store,
and reuse the client. This example reads state; it does not change a control:

```csharp
using System.Net.Http.Json;
using System.Text.Json.Serialization;

public sealed class TraderOperatorClient(HttpClient httpClient)
{
    private readonly HttpClient _httpClient = httpClient;

    public async Task<OperatorSnapshot?> GetStateAsync(
        string operatorToken,
        CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, "operator/state");
        request.Headers.Add("x-operator-token", operatorToken);
        using var response = await _httpClient.SendAsync(request, cancellationToken);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<OperatorSnapshot>(
            cancellationToken: cancellationToken);
    }
}

public sealed record OperatorSnapshot(
    [property: JsonPropertyName("application")] ApplicationState Application,
    [property: JsonPropertyName("trading")] TradingState Trading,
    [property: JsonPropertyName("connectivity")] ConnectivityState Connectivity,
    [property: JsonPropertyName("risk")] RiskState Risk,
    [property: JsonPropertyName("recovery")] RecoveryState Recovery);

public sealed record ApplicationState(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("heartbeat")] DateTimeOffset Heartbeat);

public sealed record TradingState(
    [property: JsonPropertyName("mode")] string Mode,
    [property: JsonPropertyName("credential_scope")] string CredentialScope,
    [property: JsonPropertyName("strategy_version")] string StrategyVersion);

public sealed record ConnectivityState(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("detail")] string Detail,
    [property: JsonPropertyName("checked_at")] DateTimeOffset? CheckedAt);

public sealed record RiskState(
    [property: JsonPropertyName("kill_switch")] string KillSwitch);

public sealed record RecoveryState(
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("detail")] string Detail);
```

The JSON uses snake_case, so the example maps names explicitly. It intentionally
models only the top-level health and safety fields; extend it for portfolio,
orders, fills, signals, reconciliation, alerts, and errors. Treat unknown enum
values and missing required fields as a safe failure, not as `running` or
healthy.

For control calls, make the intent explicit in the method name, require a
separate authorization decision for re-arm, set timeouts, and audit the outcome.
Do not automatically re-arm after a successful health poll.

## Processing and safety flow

The intended trading path is:

```text
closed market data
  -> pure strategy signal
  -> persisted signal and ordered risk evaluation
  -> approved RiskApproval
  -> persisted PENDING_SUBMIT order and unique client_order_id
  -> broker submission
  -> status/fill persistence
  -> broker-authoritative portfolio reconciliation
```

At any uncertainty boundary, stop progressing forward:

- stale or missing market/risk input: refuse;
- order-persistence failure: do not call the broker;
- ambiguous submission: mark `UNKNOWN` and query before retry;
- database failure after a broker call: halt and recover;
- reconciliation difference: adopt the broker record, alert, and halt;
- unreadable or invalid external kill-switch flag: halt.

## Development validation

Run the repository's lightweight required gates from the isolated worktree:

```powershell
ruff format --check .
ruff check .
mypy core brokers data strategy risk execution portfolio api app db
pytest --cov=. --cov-report=term-missing --cov-fail-under=80
git diff --check
```

The coverage threshold is a safety baseline; do not lower it to make a change
green. Tests use deterministic fixtures, `SimulatedBroker`, temporary state,
and Gemini Sandbox only when explicitly owner-run. Required GitHub checks are
green only after they complete against the exact final pull-request head SHA.

Report evidence in three separate categories:

1. **Local validation:** commands and results from the isolated worktree.
2. **Remote CI:** completed required checks for the exact pull-request head.
3. **Owner-run verification:** credentialed provider checks, real soaks, or
   infrastructure drills, with secrets and account information redacted.

Passing unit tests or CI never proves live-provider behavior.

## Change protocol for AI agents and contributors

Before editing a card, inspect the repository, full issue, dependencies,
comments, claims, branches, worktrees, and open pull requests as specified in
`AGENTS.md`. Claim one eligible card, re-read for competing claims, and work in
a dedicated worktree based on refreshed `origin/main`.

During implementation:

- preserve unrelated files, branches, worktrees, stashes, and untracked work;
- stage only the claimed paths;
- keep public CI lightweight unless the card explicitly requires more;
- never use live credentials or publish sensitive evidence;
- cover refusal, stale-input, timeout, duplicate, and recovery paths; and
- never weaken a safety gate merely to make a check pass.

Open a draft pull request only after the claimed scope is implemented and
locally checked. Do not merge, release, deploy, or enable live trading unless
the card and the user explicitly authorize that next step.

## Deployment and recovery

The production-style deployment uses `docker-compose.yml` plus
`deploy/docker-compose.vps.yml`. Migrations run before the app process accepts
requests. PostgreSQL and kill-switch state live in named volumes.

Use the exact reviewed commit and follow
`docs/phase-1.8-deployment-backups-restore.md`. Infrastructure verification
uses `deploy/drill.sh` and the `Restore drill` workflow. Do not improvise
commands against the production database, and do not reboot a host without the
owner's immediate approval for that one interruption.

Nightly backups are encrypted before leaving the host. A backup is not proven
usable until `deploy/restore-verify-postgres.sh` restores it into a separate
scratch database and verifies the migration version and durable table counts.
Never restore a drill backup over the production database.

## Troubleshooting reference

| Symptom | Likely contract | Safe next step |
| --- | --- | --- |
| Service refuses non-live startup with trade scope | Non-live modes reject `CREDENTIAL_SCOPE=trade`. | Correct the scope; do not weaken the guard. |
| Live startup refuses explicit configuration | Confirmation, broker choice, declared scope, or Coinbase-reported permissions failed. | Keep live disabled; review every guard and provider permission. |
| `503 operator authentication is not configured` | `OPERATOR_TOKEN` is absent. | Configure it through approved secret handling and restart. |
| Operator gets `401` | Token/session is missing or invalid. | Use the login form or correct header; do not put the token in the URL. |
| Operator gets `403` on re-arm | Operator token lacks administrator role. | Obtain deliberate administrator authorization; do not bypass the route. |
| Startup recovery is `no_broker` | No broker is configured and no pending order exists. | Expected only for an intentionally credential-free environment. |
| Startup recovery halts | Pending order, provider outage, missing baseline, database read failure, or divergence. | Keep halted and reconcile against the broker. |
| Broker says unavailable but old balances remain visible | Last-known values are retained only for diagnosis. | Do not treat them as current; pause or halt as risk requires. |
| Reconciliation interval is rejected | Value is non-numeric, zero/negative, or above 3600 seconds. | Set a valid bounded interval. |
| Kill switch halts after reading a flag | Flag was `halted`, invalid, or unreadable. | Fix the external flag source, investigate, then manually re-arm. |
| `/health` is OK but dashboard is degraded | Liveness does not include broker or strategy health. | Investigate detailed authenticated state. |
| Strategy remains `unknown` | Runtime has not registered and heartbeated the strategy. | Treat strategy activity as unproven; complete runtime wiring. |

## Related documents

- [`README.md`](../README.md) — project status and quick start.
- [`AGENTS.md`](../AGENTS.md) — mandatory repository and concurrent-work rules.
- [`docs/foundation.md`](foundation.md) — foundation contracts.
- [`docs/phase-1.7-operator-surface.md`](phase-1.7-operator-surface.md) — operator authentication and state contract.
- [`docs/phase-1-gate-acceptance.md`](phase-1-gate-acceptance.md) — acceptance evidence and remaining owner-run gaps.
- [`docs/phase-1.8-deployment-backups-restore.md`](phase-1.8-deployment-backups-restore.md) — deployment, backup, restore, and restart runbook.
- [`docs/phase-1.8-vps-drill-checklist.md`](phase-1.8-vps-drill-checklist.md) — controlled infrastructure evidence procedure.
- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting and sensitive-data boundaries.

## Manual maintenance checklist

Update this manual whenever a release changes any of the following:

- supported modes, brokers, credentials, or startup guards;
- dashboard fields, authentication, routes, or control roles;
- trading-loop, strategy-heartbeat, P/L, or alert runtime wiring;
- order recovery or reconciliation semantics;
- environment variables or deployment topology;
- backup, restore, or incident procedures; or
- owner-run evidence and live-activation status.

Verify the manual against current source and tests rather than copying planned
behavior from a roadmap.
