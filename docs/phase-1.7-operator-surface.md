# Phase 1.7 operator surface

The operator surface is an authenticated, server-rendered view of trading state
and emergency controls. It is deliberately provider-neutral and accepts an
optional `BrokerInterface` and alert sinks from the application boundary.

## Authentication and authorization

- `OPERATOR_TOKEN` is required for operator access.
- `OPERATOR_ADMIN_TOKEN` is optional. When configured, re-arm requires the admin
  token while pause and emergency stop remain available to the operator token.
- `GET /operator/login` and its form POST establish an HttpOnly, SameSite
  session cookie. The dashboard never places a token in an HTML form action or
  rendered page. Header authentication (`x-operator-token`) remains available
  for API clients.
- `GET /health` is liveness-only and does not expose trading state. Detailed
  health and strategy heartbeats require operator authentication.

## State and degraded behavior

`GET /operator` renders the dashboard, and `GET /operator/fragment` provides
the HTMX refresh fragment. `GET /operator/state` returns the same state as JSON.
The view includes trading mode, strategy version, application and broker
connectivity, strategy heartbeats, kill-switch state, balances, positions, P/L
availability, orders, fills, signals, alerts, and errors.

When the broker cannot be read, the surface reports `unavailable`, preserves any
previous values only as explicitly-labelled last-known data, and records a
redacted error condition. It never presents stale broker data as a current
successful refresh and never includes provider payloads or credentials.

## Controls and alert delivery

- `POST /operator/pause` sets the persistent kill switch to `paused`.
- `POST /operator/emergency-stop` sets it to `halted`.
- `POST /operator/rearm` requires administrator authorization when an admin
  token is configured.
- The `AlertRouter` fans out an alert to the explicitly injected phone-push and
  email sinks and records per-destination delivery status. Automated tests use
  deterministic recording sinks; no provider writes occur by default.
