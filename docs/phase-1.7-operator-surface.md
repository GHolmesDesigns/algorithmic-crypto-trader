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
  for API clients; a header request receives no cookie.
- `POST /operator/logout` revokes the session on the server and clears its
  cookie, so even a copy of the cookie is refused afterwards. Revocations are
  held in memory until the session would have expired; a restart forgets them,
  by which point the signed-out browser has already dropped its cookie.
- A `token` query parameter is refused with `400` on every authenticated route
  (decision for issue #60). Query tokens end up in browser history, proxy logs,
  and referrers, and no browser flow or script needs them: the browser uses the
  form and cookie, and scripts, including `deploy/drill.sh`, use the header.
- `GET /health` is liveness-only and does not expose trading state. Detailed
  health and strategy heartbeats require operator authentication.

## State and degraded behavior

`GET /operator` renders the dashboard, and `GET /operator/fragment` returns its
body without the page shell. The page does not refresh itself. `GET /operator/state` returns the same state as JSON.
The view includes trading mode, strategy version, application and broker
connectivity, strategy heartbeats, kill-switch state, balances, positions, P/L
availability, orders, fills, signals, alerts, and errors.

When the broker cannot be read, the surface reports `unavailable`, preserves any
previous values only as explicitly-labelled last-known data, and records a
redacted error condition. It never presents stale broker data as a current
successful refresh and never includes provider payloads or credentials.

## Controls and alert delivery

- `POST /operator/pause` sets the persistent kill switch to `paused`; it never
  lowers `halted`.
- `POST /operator/emergency-stop` sets it to `halted`.
- Each control, and sign-out, ends on a server-rendered result page (or JSON for
  a client that does not accept HTML): the action, resulting state, whether it
  changed, the UTC time, the acting role, the next safe step, and a return link.
- `GET /operator/rearm` is the administrator re-arm review. It repeats the
  startup recovery, reconciliation, pending or unknown order, and strategy
  heartbeat warnings, lists recent kill-switch changes, and shows the seven
  checklist items from the user manual as required checkboxes with a required
  cause-and-approval reference.
- `POST /operator/rearm` requires the administrator role. The server refuses,
  with the kill switch unchanged, a re-arm missing any checklist item or the
  reference (`422`), from an operator (`403`), or whose transition cannot be
  saved (`503`). The reference is redacted before it is saved.

## Kill-switch history

Every transition is saved to `system_events` (`event_type`
`kill_switch_transition`) with its previous and new state, the acting role
(`operator`, `admin`, or `system`), whether it was automatic, the reason, the
confirmed checklist for a re-arm, and the UTC time. The service lifespan attaches
this journal before startup recovery, so a recovery halt is saved, and loads the
recent history, which `/operator/state` returns newest first under
`risk.transitions`.

A stop never waits on the database: it takes effect and persists to the state
file first, and a save that fails is retried with the next transition. A re-arm
is saved first, together with any unsaved earlier transitions, and is refused
when that fails or no journal is attached.
- The `AlertRouter` fans out an alert to the explicitly injected phone-push and
  email sinks and records per-destination delivery status. Automated tests use
  deterministic recording sinks; no provider writes occur by default.
