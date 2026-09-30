# New features build report: Algorithmic Crypto Trader

- **Prepared:** 2026-09-27
- **Current merged baseline:** `origin/main` at `15e168a`
- **Current active implementation:** draft PR `#54` at `a8a438a`
- **Companion document:** `docs/ui-design-handoff.md`
- **Audience:** agents implementing the next feature iterations
- **Scope:** implementation plan and engineering contracts, not authorization to place live orders

## 1. Purpose

This report converts the product and UI roadmap into buildable engineering workstreams. It names dependencies, proposed interfaces, persistence needs, safety constraints, failure paths, validation, and evidence requirements.

It does not create or claim cards. Before implementation, each workstream must have an eligible issue with a complete outcome, dependencies, acceptance criteria, and safety section. The implementing agent must follow `AGENTS.md`, claim one card, and use a dedicated worktree.

## 2. Status vocabulary

| Label | Meaning |
| --- | --- |
| **Merged** | Present on `origin/main` at the baseline above. |
| **Draft** | Implemented on an open pull request but not yet merged. |
| **Backlog** | Recorded in an open repository issue or the accepted planning document. |
| **Proposed** | Feasible and recommended, but a card must be approved before implementation. |

Do not build a dependency from draft behavior as though it were merged. Either wait for the predecessor or use an explicitly documented stacked branch.

## 3. Current live planning state

The live repository state checked for this report is:

| Record | State | Purpose |
| --- | --- | --- |
| Issue `#51` / PR `#54` | **Draft** | Run the paper strategy from public Coinbase market data and send operator alerts. |
| Issue `#30` | **Backlog** | Represent unknown position cost basis as absent rather than zero. |
| Issue `#12` | **Backlog** | Complete the 30-day unattended paper soak. |
| Issue `#13` | **Backlog** | Controlled Coinbase live activation after the soak. |
| Issue `#14` | **Backlog** | Decide and plan the next venue after Coinbase is stable. |

There is no approved issue yet for the full operator UI redesign, operational activity history, research workspace, or readiness console proposed in the companion UI report.

## 4. Dependency map

```text
origin/main (15e168a)
|
+-- PR #54 / Issue #51: paper runtime + alerts --------------------+
|                                                                  |
+-- Issue #30: explicit unknown cost basis --------------------+    |
|                                                              |    |
|                                                              v    v
|                                                   truthful operator dashboard
|                                                              |
|                                  +---------------------------+------------------+
|                                  |                           |                  |
|                                  v                           v                  v
|                         operational history          research/replay UI   soak instrumentation
|                                                                                |
|                                                                                v
|                                                                  Issue #12: 30-day soak
|                                                                                |
|                                                                                v
|                                                                  Issue #13: controlled live
|                                                                                |
|                                                                                v
+------------------------------------------------------------------ Issue #14: venue decision
```

Critical ordering rules:

1. Do not start a new card that modifies `api/operator.py`, `api/alerts.py`, `api/templates/operator_fragment.html`, `app/main.py`, or the paper-runtime contract while PR `#54` is active unless the work is explicitly stacked on it.
2. Land issue `#30` before any feature treats position average price as cost basis or calculates per-position P/L.
3. The runtime and alert path must be merged and deployed in paper mode before issue `#12` begins counting soak time.
4. Phase 2 live work cannot begin until issue `#12` satisfies every exit criterion.
5. Phase 3 adapter work cannot begin until issue `#13` exits and issue `#14` records a separate venue decision.

## 5. Cross-cutting architecture contracts

Every new feature must preserve these contracts.

### 5.1 Provider neutrality

- Keep provider-specific request, authentication, payload, and order-edit behavior inside `brokers/`.
- UI and read models should use broker capabilities and environment labels rather than hard-coded Coinbase assumptions.
- Strategy, risk, execution, portfolio, and reconciliation must not branch on provider-specific payload shapes.
- A future venue must pass the unchanged shared broker contract suite.

### 5.2 Safety and authority

- Strategy code returns a signal and cannot submit an order.
- Execution accepts only a matching approved `RiskApproval`.
- Persist the deterministic client order ID before any broker submission.
- An ambiguous submission is `unknown`; query by client order ID before retry.
- Persist authoritative fills before accepting terminal order state.
- Missing, stale, unreadable, or inconsistent safety input fails closed.
- The broker remains authoritative during reconciliation.
- External kill-switch inputs may tighten state but may never re-arm.
- No new browser endpoint may submit arbitrary orders.

### 5.3 Browser architecture

- Retain FastAPI, Jinja, and server-rendered fallbacks unless an approved card changes the frontend architecture.
- Safety controls must work without JavaScript.
- If HTMX is used, package it locally or serve a pinned local asset. Emergency controls must not depend on a CDN.
- Prefer additive JSON fields and stable server-rendered HTML over duplicating business rules in client JavaScript.
- Keep all browser mutations authenticated and explicit.
- Never place authentication tokens in URLs.
- Add browser-side behavior only when the server remains authoritative for authorization and state transitions.

### 5.4 Read models and pagination

- Do not expose ORM records directly.
- Create explicit response models with only the fields the UI needs.
- Historical collections must be bounded and paginated.
- Apply status, symbol, time, and correlation filters in the database before pagination.
- Use a stable cursor, preferably `(created_at, primary_key)`, so equal timestamps do not skip or duplicate rows.
- Return source timestamps and freshness, not only formatted display strings.
- Redact provider payloads and secret-like fields before persistence and again before response serialization.

### 5.5 Evidence model

Keep these evidence classes separate in code and UI:

1. local automated validation;
2. completed remote CI on the exact pull-request head;
3. agent-run infrastructure verification;
4. owner-run exchange/provider verification.

A fixture, mock, skipped test, plan-only run, or pending CI check is not live-provider evidence.

## 6. Workstream 0: finish the active paper runtime and alert implementation

- **Status:** Draft issue `#51`, PR `#54`
- **Current owner:** the existing claim/branch; another agent must not duplicate it
- **Primary branch:** `feat/51-paper-runtime-alerts`

### 6.1 Outcome

Run the unchanged paper strategy, risk, execution, and reconciliation path continuously from public Coinbase market data, with operator-visible state and configured phone/email alerts.

### 6.2 Draft implementation already present

- `app/paper_runtime.py` composes the runtime.
- Public Coinbase ticker and five-minute candle channels feed closed-candle state.
- Disconnects gap-fill before strategy processing resumes.
- `MovingAverageCrossStrategy` enters the existing `TradingCycle`.
- The runtime shares the reconciler's execution engine and lock.
- Operator state gains runtime status, detail, last cycle status, and last cycle time.
- ntfy and SMTP sinks have bounded timeouts and configuration validation.
- Deployment and manual documentation include paper-runtime and alert settings.

### 6.3 Remaining completion work

Before merge, the owning agent must:

- rebase or merge the latest `origin/main` according to repository policy;
- run all repository gates in the isolated worktree;
- resolve every failure without weakening refusal paths;
- verify the final diff remains paper-only;
- push the finalized head;
- require completed remote checks for that exact head SHA;
- retain draft status if owner-run acceptance evidence required by the card remains incomplete;
- record remaining provider/soak evidence honestly.

### 6.4 UI contract introduced by the draft

Expected additive `operator/state` data:

```json
{
  "runtime": {
    "status": "not_started | running | degraded | stopped | failed",
    "detail": "redacted human-readable detail",
    "last_cycle_status": "string or null",
    "last_cycle_at": "UTC timestamp or null"
  }
}
```

The consuming UI must handle unknown future status values safely and render them as unrecognized/degraded rather than healthy.

### 6.5 Required failure coverage

- runtime disabled;
- wrong trading mode;
- missing broker;
- invalid symbol or window configuration;
- missing history;
- stale or missing quote;
- WebSocket disconnect and exact-once gap fill;
- terminal market-data failure;
- persistence failure;
- reconciliation lock contention;
- runtime shutdown during network activity;
- partial alert configuration;
- ntfy failure;
- SMTP failure;
- alert delivery failure without secret leakage.

## 7. Workstream 1: explicit unknown cost basis

- **Status:** Backlog issue `#30`
- **Dependency:** merged Phase 1 gate implementation
- **Blocks:** trustworthy average-price display, position P/L, and tax-lot work

### 7.1 Outcome

Represent a provider position with unknown cost basis as `None` from adapter through storage, reconciliation, ledger, API, and UI.

### 7.2 Code changes

Expected affected areas:

- `core/models.py`: `Position.average_price` becomes optional.
- `brokers/coinbase.py`: provider positions return `None` when basis is unknown.
- `brokers/gemini.py`: provider positions return `None` when basis is unknown.
- `brokers/simulated.py`: retain calculated average price.
- `portfolio/store.py`: persist and load nullable average price.
- `portfolio/reconciliation.py`: compare basis only when both sides know it.
- `portfolio/ledger.py`: buying onto an unknown basis keeps the result unknown; selling preserves unknown basis.
- `app/trading.py`: confirm exposure valuation still uses current bid rather than cost basis.
- `api/templates/operator_fragment.html`: render “Unavailable” or “Unknown,” never zero.

### 7.3 Migration

Create the next Alembic revision:

- make `positions_snapshot.average_price` nullable;
- convert legacy zero values to `NULL` during upgrade;
- restore `NULL` to zero only for downgrade compatibility;
- test upgrade and downgrade against legacy rows;
- restore and verify a backup created before the migration.

### 7.4 Acceptance criteria

- Unknown cost basis is `None` in every layer.
- A known zero is not silently invented.
- A buy after adopting an unknown broker baseline does not produce a diluted average.
- Two known, unequal average prices still create a discrepancy.
- Known versus unknown basis does not create a false discrepancy.
- Quantity and balance mismatches remain strict and still halt.
- Operator HTML and JSON distinguish unknown from zero.
- Migration and backup/restore evidence pass on the exact PR head.

### 7.5 Tests

- model serialization and validation;
- Coinbase and Gemini adapter mapping;
- simulator known basis;
- SQL round trip for `NULL`;
- migration upgrade/downgrade;
- reconciliation known/unknown matrix;
- ledger buy and sell behavior;
- operator rendering;
- trading risk valuation at current bid;
- restart and restore drill.

## 8. Workstream 2: operator UI foundation

- **Status:** Proposed; create a card before implementation
- **Dependency:** PR `#54` should merge first because it overlaps the operator templates and state model
- **Recommended size:** split into foundation and dashboard cards if the final scope exceeds one reviewable PR

### 8.1 Outcome

Create an accessible, responsive product shell around the existing authenticated operator surface without changing trading behavior.

### 8.2 Foundation deliverables

- Local application stylesheet derived from the existing manual tokens.
- Semantic page shell with header, navigation or section rail, main landmark, skip link, and role label.
- Responsive one-column and multi-column layouts.
- Local HTMX asset or removal of nonfunctional HTMX attributes.
- Partial-refresh boundaries that preserve focus and do not repeatedly announce the full page.
- Reusable status badge, timestamp/freshness, metric, table, empty, unavailable, and alert components implemented as Jinja macros or includes.
- Explicit light/dark system themes and reduced-motion behavior.
- Server-rendered control-result page using the same shell.
- Login page using the same visual language without exposing configuration detail.

### 8.3 Proposed security additions

Create separate acceptance criteria for these changes rather than hiding them inside styling work:

- `POST /operator/logout` clears the session cookie.
- Browser forms include CSRF protection appropriate to the signed cookie model.
- Header-token API clients remain supported without browser CSRF ceremony.
- Remove browser reliance on query-string token compatibility; consider deprecating server acceptance in a separate compatibility card.
- Add `Cache-Control: no-store` to authenticated operator pages and state responses.
- Apply a restrictive Content Security Policy compatible with local assets and HTMX.

### 8.4 Acceptance criteria

- Login, dashboard, pause, emergency stop, re-arm, control confirmation, and logout work with keyboard navigation.
- Pause, stop, and re-arm work with JavaScript disabled.
- No token appears in HTML, URL, CSS, JavaScript, response body, or browser history.
- Layout works at 320 CSS pixels and common desktop widths.
- Focus remains visible and logical after partial refresh.
- Status meaning does not depend on color alone.
- The page remains usable when CSS, JavaScript, or one fragment request fails.
- Existing authorization tests remain green.

### 8.5 Suggested files

- `api/templates/base.html` — proposed shared shell.
- `api/templates/operator_login.html`.
- `api/templates/operator.html`.
- `api/templates/operator_fragment.html`.
- `api/templates/operator_control_result.html` — proposed replacement for inline HTML.
- `api/templates/macros/` — proposed reusable components.
- `api/static/operator.css`.
- `api/static/htmx.min.js` if HTMX is retained.
- `api/routes.py`.
- `tests/test_app.py` and focused template/security tests.

## 9. Workstream 3: truthful operator command center

- **Status:** Proposed
- **Dependencies:** workstream 0 merged; workstream 1 merged before cost-basis display; UI foundation complete

### 9.1 Outcome

Render all high-value state already available from `GET /operator/state`, with source, timestamp, and certainty visible at the point of use.

### 9.2 Merged data to surface

- application status and heartbeat;
- trading mode and credential scope;
- strategy version;
- broker connectivity, detail, and check time;
- kill-switch state;
- startup recovery status, detail, counts, and completion time;
- reconciliation run totals, last result, last run, and discrepancy count;
- portfolio status and data-source explanation;
- balances with available, held, and timestamp;
- positions with quantity, nullable average price, and timestamp;
- explicit P/L unavailable state;
- orders with nested request terms, status, fill progress, and times;
- fills with fees and times;
- signals with strategy and correlation IDs;
- strategies with heartbeat age;
- alerts with destination delivery state;
- redacted errors;
- page snapshot time.

### 9.3 Draft data to surface after PR #54

- paper runtime status and detail;
- last cycle status and time;
- strategy runtime heartbeat;
- configured alert sinks.

### 9.4 Backend changes

Prefer no new business logic. Add only display-oriented fields that the server can derive authoritatively, such as:

- an explicit environment/provider label;
- data-source names;
- server-calculated freshness state where a contractual threshold exists;
- stable display-safe status enums.

Do not calculate trading eligibility in the browser. Do not infer health from absence of errors.

### 9.5 Acceptance states

Tests and design fixtures must cover:

- no broker configured;
- healthy paper broker;
- broker unavailable with retained last-known values;
- stale timestamps;
- startup recovery not run, reconciled, and halted;
- reconciliation not scheduled, clean, diverged, and unavailable;
- runtime not started, running, degraded, and failed;
- kill switch running, paused, and halted;
- operator versus administrator;
- empty and populated activity;
- unknown cost basis;
- P/L unavailable;
- alert sent and alert delivery failed.

### 9.6 Performance contract

- One dashboard refresh must not trigger one database/provider call per row.
- Provider refresh remains bounded and redacted.
- Large collections must be summarized on the overview; do not render unbounded order/fill histories.
- Partial refresh should replace only the live-state region.
- Add response-time observations in tests, but avoid fragile wall-clock assertions.

## 10. Workstream 4: operational activity and diagnostics

- **Status:** Proposed
- **Dependency:** command-center read model and stable audit persistence
- **Risk:** this work adds new data-access surfaces; it must remain read-only

### 10.1 Outcome

Let an operator or engineer trace why the system acted or refused to act without querying the database manually.

### 10.2 Proposed read endpoints

Exact route names require a card-level decision. A coherent option is:

| Method and route | Purpose |
| --- | --- |
| `GET /operator/activity` | Server-rendered, filtered activity page. |
| `GET /operator/activity/data` | Paginated JSON activity envelope. |
| `GET /operator/orders/{client_order_id}` | Linked order detail. |
| `GET /operator/correlations/{correlation_id}` | Signal-to-fill trace. |
| `GET /operator/discrepancies` | Paginated reconciliation evidence. |
| `GET /operator/events` | Paginated redacted system events. |

All routes require operator authentication. No write route belongs in this workstream.

### 10.3 Proposed activity envelope

```json
{
  "items": [],
  "next_cursor": "opaque or null",
  "filters": {
    "symbol": null,
    "status": null,
    "failed_gate": null,
    "correlation_id": null,
    "from": null,
    "to": null
  },
  "warnings": []
}
```

Normalize missing optional collections to empty arrays at the server boundary.

### 10.4 Required data joins

Build an explicit query/service layer that links:

- `signals.signal_id`;
- `risk_decisions.signal_id` and `approval_id`;
- `orders.signal_id`, `risk_approval_id`, `client_order_id`, and `correlation_id`;
- `fills.order_id` and broker fill ID;
- `system_events.correlation_id`;
- discrepancies by entity type/key and time.

Do not guess links when an identifier is missing. Surface an audit-lineage warning.

### 10.5 UI capabilities

- filters for symbol, order status, failed gate, time, and correlation ID;
- compact event timeline;
- expandable raw domain fields, never raw provider payloads;
- order fill progress;
- risk approval/refusal reason and failed gate;
- unknown/pending-order emphasis;
- discrepancy before/authoritative-after comparison with safe redaction;
- copy controls for safe identifiers;
- server-side pagination.

### 10.6 Failure tests

- invalid cursor;
- cursor tied to different filters;
- missing lineage;
- unknown identifiers;
- equal timestamps;
- empty pages;
- large histories;
- malformed JSON payload already stored in a legacy event;
- redaction of secret-like keys;
- database unavailable;
- unauthorized and operator/admin access behavior.

## 11. Workstream 5: soak instrumentation and readiness console

- **Status:** Backlog issue `#12` plus proposed supporting cards
- **Dependencies:** paper runtime deployed; alerts working; VPS and restore evidence complete
- **Elapsed criterion:** 30 consecutive days; engineering estimates must not treat this as a 30-day coding task

### 11.1 Outcome

Produce durable, reviewable evidence that paper operation is stable over time, and make the exit criteria visible without weakening them.

### 11.2 Required daily digest

The accepted plan requires:

- equity;
- day P/L;
- trades;
- risk rejections by gate;
- reconciliation status;
- error counts;
- uptime.

The digest must be generated from persisted authoritative data. Browser-local aggregation is insufficient.

### 11.3 Proposed persistence

Do not add all tables automatically; validate the read requirements first. A feasible minimal model is:

#### `daily_operations_digest`

- digest date and reporting timezone;
- generated-at timestamp;
- opening/closing equity and day P/L status/value;
- trade count;
- rejection counts by gate as bounded JSON;
- reconciliation run/clean/diverged/unavailable counts;
- error count;
- uptime seconds;
- source window start/end;
- strategy version and application commit;
- completeness status and warning list.

#### `operational_incident`

- incident ID;
- detected/resolved timestamps;
- category and severity;
- redacted summary;
- status;
- evidence references;
- affected soak criterion;
- whether the affected criterion clock reset.

#### `verification_evidence`

- evidence ID;
- criterion key;
- evidence class: automated, exact-head CI, agent-run infrastructure, owner-run provider;
- observed-at timestamp;
- commit SHA;
- redacted result and reference;
- pass/fail/partial state.

If an existing event/audit model can satisfy the requirement cleanly, prefer extending a read projection over duplicating canonical facts.

### 11.4 Proposed endpoints and screens

- `GET /operator/soak` — current period and criterion status.
- `GET /operator/soak/digests` — paginated daily digests.
- `GET /operator/incidents` — incident list and detail.
- `GET /operator/readiness` — read-only readiness projection.

No readiness page may mutate safety state or authorize live trading.

### 11.5 Soak criterion engine

Implement a read-only projection that reports each criterion as:

- `not_started`;
- `collecting`;
- `passed`;
- `failed_reset_required`;
- `blocked`;
- `needs_review`.

The projection should name the evidence window and missing evidence. It must never auto-waive a criterion.

### 11.6 Required operational events

Capture or attach evidence for:

- real WebSocket disconnect and successful gap fill;
- process restart and broker-state recovery;
- reconciliation divergence observed or deliberately injected and handled;
- kill-switch firing during the period;
- weekly restart drills;
- backup creation and scratch restore;
- every incident, including self-resolving anomalies.

### 11.7 Acceptance criteria

- Digest generation is idempotent for a reporting day.
- A partial data day is marked incomplete, not silently zero-filled.
- Timezone boundaries are explicit and tested around DST.
- Rejections are grouped by stable gate keys.
- Uptime is derived from durable process/runtime evidence.
- Incident resolution never deletes original evidence.
- A failed criterion records why its clock reset.
- The UI cannot mark a criterion passed without qualifying evidence.
- The 30-day clock begins only after documented entry criteria are satisfied.

## 12. Workstream 6: research and replay workspace

- **Status:** Proposed
- **Dependency:** none on live activation; can proceed after a separate product card and data-handling decision
- **Safety:** preview/research only; no provider writes

### 12.1 Outcome

Expose the existing backtest, walk-forward, and replay capabilities through a controlled interface that produces reproducible reports.

### 12.2 Proposed features

- Select an approved stored candle dataset or recorded stream.
- Choose a registered strategy and immutable strategy version.
- Configure initial cash, fees, spread, slippage, partial-fill ratio, and walk-forward windows.
- Validate that the final holdout remains sealed until the intended run.
- Queue a bounded backtest/replay job.
- Show job progress and failure reason.
- Render mandatory report fields.
- Compare versioned runs.
- Export a redacted JSON/HTML report.

### 12.3 Backend boundary

Do not execute long-running work inside a synchronous request. Introduce a bounded job model or worker only after deciding:

- maximum dataset size;
- concurrent job limit;
- timeout/cancellation behavior;
- storage retention;
- resource isolation;
- whether uploads are allowed or datasets must be pre-registered;
- how strategy code is selected without accepting arbitrary executable code.

### 12.4 Proposed job records

- job ID;
- job type: backtest or replay;
- requested and completed timestamps;
- status;
- strategy version/hash;
- dataset identity and immutable hash;
- configuration snapshot;
- report reference;
- redacted failure detail;
- requester role.

### 12.5 Acceptance criteria

- Identical dataset, strategy hash, and configuration produce deterministic results.
- Monetary configuration and results remain decimal-safe.
- No look-ahead guard can be disabled through the UI without an explicit test-only contract.
- The sealed holdout cannot leak into training windows.
- A canceled or failed job performs no provider writes.
- Reports label backtests as research, not profitability evidence.
- Large datasets cannot exhaust the web process.
- Exported artifacts contain no credentials or raw secret-bearing payloads.

## 13. Workstream 7: controlled Coinbase live support

- **Status:** Backlog issue `#13`
- **Hard dependency:** issue `#12` completed in full
- **Authorization:** a separate explicit owner decision immediately before activation

### 13.1 Outcome

Enable and observe the production Coinbase order path with the smallest practical exposure after every safety and durability gate is satisfied.

### 13.2 Preconditions enforced outside the UI

- Phase 1.5 soak accepted.
- Coinbase key can view and trade but cannot transfer or withdraw.
- IP allowlisting is configured where supported.
- Reconciler, alerts, kill switch, backups, restore, and incident runbook are approved and tested.
- Live confirmation and mode/scope guards pass.
- No unresolved incident or pending/unknown order exists.

The UI displays preconditions but must not be their sole enforcer.

### 13.3 Required implementation capabilities

- smallest practical order-size configuration;
- strict live daily-loss and drawdown limits;
- paper and live decisions produced in parallel for comparison;
- every live fill placed into a review queue for the first two weeks;
- realized fee and slippage measurement against the versioned model;
- verified production emergency-stop exercise;
- exact account/provider environment labeling;
- immutable audit connection from decision through fill review.

### 13.4 Proposed read models

#### Paper/live comparison

- decision time;
- symbol and side;
- paper signal/risk outcome;
- live signal/risk outcome;
- difference category;
- explanation status and redacted note;
- strategy/configuration versions.

#### Live fill review

- fill and order identifiers;
- broker occurrence time;
- expected versus realized price;
- modeled versus realized fee;
- slippage;
- reconciliation state;
- review status and reviewer timestamp;
- exception note.

### 13.5 UI constraints

- “LIVE” must be persistent, high contrast, and textual.
- Show exposure and risk limits near every live summary.
- Emergency stop must remain immediately accessible.
- Do not add discretionary manual order entry.
- Do not place secret or account identifiers in rendered pages.
- Do not let the UI bypass confirmation, credential, risk, audit, or reconciliation gates.

### 13.6 Exit evidence

- two consecutive weeks of bounded Coinbase live operation;
- zero reconciliation breaks;
- every paper/live difference explained;
- realized slippage within the approved bound;
- every live fill reviewed;
- production emergency stop deliberately fired and verified.

## 14. Workstream 8: future multi-broker expansion

- **Status:** Backlog issue `#14` is decision-only
- **Dependency:** Phase 2 exit
- **Current decision:** no new venue selected

### 14.1 First deliverable: decision record

Reassess candidates using current evidence for:

- jurisdiction availability;
- spot products and market-data depth;
- order lifecycle and idempotency support;
- sandbox/test environment;
- rate-limit model;
- official SDK availability;
- credential scopes and withdrawal isolation;
- WebSocket and historical data;
- ecosystem maturity;
- operational and owner-run verification cost.

Record either the selected venue or an explicit decision to defer.

### 14.2 Implementation only after selection

A future adapter must:

- implement `BrokerInterface` and accurate `BrokerCapabilities`;
- pass the unchanged simulator-authored contract suite;
- keep host/auth/payload logic inside its adapter;
- degrade safely to polling if streaming is absent;
- use cancel-and-replace if native edit is absent;
- preserve deterministic client order IDs and ambiguity recovery;
- add separate credentials and environment guards;
- include fixtures and bounded owner-run verification;
- avoid changes to strategy, risk, portfolio, and reconciliation contracts.

### 14.3 UI preparation

- Render provider and environment from data.
- Display capability differences.
- Avoid Coinbase-specific terminology in shared tables and controls.
- Keep venue accounts visibly separated.
- Never aggregate balances across venues without explicit currency/valuation rules and freshness.

## 15. Recommended card breakdown

Do not implement the entire report in one PR. A reviewable sequence is:

1. Finish existing PR `#54`.
2. Issue `#30`: nullable cost basis and migration.
3. Proposed card: operator shell, local assets, accessibility, and logout/security headers.
4. Proposed card: truthful overview using existing operator-state fields.
5. Proposed card: bounded activity query service and JSON endpoints.
6. Proposed card: activity/timeline UI.
7. Proposed card: daily digest persistence and generator.
8. Proposed card: incident/evidence persistence and read model.
9. Proposed card: soak/readiness console.
10. Issue `#12`: execute and record the 30-day soak.
11. Proposed card: research/replay job decision and threat/resource model.
12. Proposed card(s): research job runner and UI, if approved.
13. Issue `#13`: controlled live implementation and verification.
14. Issue `#14`: venue decision record.

Each card should name exact file/contract overlap with active work. If two cards touch the same templates, state model, migration chain, or route module, run them sequentially or use an explicitly stacked branch.

## 16. Database and migration strategy

### 16.1 General rules

- Prefer additive migrations.
- Preserve old-reader compatibility when practical during deployment.
- Give every migration an upgrade and downgrade test.
- Never rewrite or delete audit history to simplify a UI.
- Use numeric/decimal database types for money.
- Store timestamps with timezone.
- Avoid unbounded JSON when normalized columns are required for filtering.
- Redact before persisting any provider-derived diagnostic object.

### 16.2 Deployment sequence for schema work

1. Verify exact reviewed commit.
2. Confirm the deployment is not `live` and credentials are not trade-capable for infrastructure drills.
3. Take the required encrypted backup using repository scripts.
4. Run the migration through the normal entrypoint.
5. Verify migration head and row invariants.
6. Start the service and inspect startup recovery.
7. Run focused read/write compatibility checks.
8. Restore the encrypted artifact into a scratch database and verify it.
9. Record redacted pass/fail evidence.

Use `deploy/drill.sh` and the Restore drill workflow. Do not improvise commands against the production database.

## 17. Authentication and web-security checklist

Every new browser feature must review:

- operator versus administrator authorization;
- cookie age, signing, SameSite, Secure, and HttpOnly flags;
- CSRF for cookie-authenticated mutations;
- no-store caching for authenticated data;
- Content Security Policy;
- no token in URL, markup, telemetry, screenshots, or client logs;
- output escaping in templates;
- bounded request bodies and query parameters;
- stable error responses that do not reveal secrets;
- safe redirect targets;
- session clearing on logout;
- brute-force/rate-limit policy for login if the surface becomes internet-reachable;
- reverse-proxy policy for `/docs`, `/redoc`, and `/openapi.json`.

Security changes should have focused tests and their own acceptance criteria rather than being assumed from visual QA.

## 18. Testing strategy

### 18.1 Unit and component tests

- response-model serialization;
- status mapping;
- nullable/unknown handling;
- Jinja macro output and escaping;
- auth role visibility;
- pagination cursors and filters;
- aggregation and digest idempotency;
- timezone/DST boundaries;
- redaction.

### 18.2 Integration tests

- FastAPI authenticated routes;
- JavaScript-free control posts;
- SQLAlchemy query/read models;
- migration upgrade/downgrade;
- runtime-to-operator-state updates;
- alert delivery recording;
- activity lineage joins;
- database outage and degraded render paths.

### 18.3 Browser-level checks

If a browser test framework is added, keep the first suite focused:

- login and logout;
- operator versus admin controls;
- pause, halt, and re-arm confirmations;
- keyboard focus order;
- narrow viewport;
- fragment refresh without focus loss;
- degraded/unknown/last-known rendering;
- no token in the location or DOM.

Do not add a large frontend toolchain merely to test static markup unless the card justifies its maintenance cost.

### 18.4 Failure and refusal coverage

For every feature, identify at least:

- missing data;
- stale data;
- invalid input;
- duplicate/idempotent retry;
- timeout;
- database unavailable;
- provider unavailable;
- partial completion;
- unauthorized role;
- secret-redaction failure;
- restart/recovery behavior.

## 19. Repository validation commands

Run from the isolated worktree with the checkout installed in editable mode:

```text
python -m pip install -e ".[dev]"
python -m ruff format --check .
python -m ruff check .
python -m mypy core brokers data strategy risk execution portfolio api app db probes
python -m pytest --cov=. --cov-report=term-missing --cov-fail-under=80 --basetemp=<isolated-temp-path>
git diff --check
```

On Windows, use `python -m` tooling. If the controlled process-kill tests hit the known stdin-handle platform failure, run the relevant pytest command through `cmd.exe` with input redirected from `NUL`, and report the platform workaround separately from application results.

Focused success does not replace the repository-wide gates. Remote CI counts only when required checks complete successfully for the exact finalized pull-request head.

## 20. Evidence and rollout matrix

| Feature | Local validation | Exact-head CI | Agent-run infrastructure | Owner-run provider |
| --- | --- | --- | --- | --- |
| PR `#54` paper runtime | Full gates, disconnect/shutdown/sink fakes | Required | Deployment/restart where card requires | Real stream/soak remains separate |
| Issue `#30` cost basis | Full gates, migration, restore fixtures | Required | Restart and restore drill | None unless provider mapping needs confirmation |
| UI foundation/dashboard | Route/template/security/accessibility tests | Required | Deployment smoke | None |
| Activity/read models | Query, auth, pagination, redaction tests | Required | Deployment smoke if schema changes | None |
| Soak instrumentation | Aggregation, restart, migration, failure tests | Required | VPS, backup, restore, reboot with approval | Exchange observations during soak |
| Research workspace | Determinism, isolation, resource limits | Required | Worker/deployment smoke if introduced | None |
| Controlled live | Full fail-closed suite | Required | Reviewed commit deployment | Required and explicitly authorized |
| Future adapter | Contract suite and fixtures | Required | Deployment as scoped | Required, bounded, venue-specific |

## 21. Observability requirements

New features must produce enough evidence to diagnose failure without exposing secrets.

### 21.1 Required identifiers

- correlation ID;
- signal ID;
- approval ID;
- client order ID;
- safe broker order/fill reference where allowed;
- strategy version/hash;
- application commit/release;
- source timestamp and observation timestamp.

### 21.2 Required status signals

- producer status;
- last successful update;
- last attempted update;
- stale threshold/status;
- error condition key;
- redacted detail;
- retry/recovery status;
- alert-delivery status.

### 21.3 Prohibited observability content

- API keys or private keys;
- authorization headers, signatures, or JWTs;
- operator/admin tokens;
- private ntfy topics or recipient addresses;
- raw provider payloads containing account data;
- hostnames, IP addresses, bucket names, or production identifiers in public records;
- database URLs or dumps.

## 22. Performance and capacity boundaries

Each new card should state its bounds explicitly:

- maximum page size;
- maximum cursor scan window;
- maximum date range;
- maximum dataset/job size;
- maximum concurrent background jobs;
- maximum provider request budget;
- alert timeout;
- runtime reconnect/backoff ceiling;
- digest generation timeout;
- retention period.

Fail closed or return a bounded validation error when a limit is exceeded. Do not silently truncate safety evidence without a visible warning.

## 23. Documentation updates per feature

Update documentation in the same PR when contracts change:

- `README.md` for top-level capability and release boundaries;
- `docs/user-manual.md` for operator and engineering procedures;
- relevant phase/evidence document;
- `.env.example` and deployment environment documentation for configuration;
- runbooks for startup, recovery, backup, restore, incidents, and owner-run checks;
- this build report or its successor if dependencies change.

The operator manual must continue to separate non-technical instructions from engineering/agent detail and distinguish implemented, planned, and unverified behavior.

## 24. Pull-request handoff template

Every feature PR should state:

- issue and dependency chain;
- exact behavior changed;
- files and contracts affected;
- migrations and compatibility;
- local validation and results;
- exact final commit SHA;
- safety and reconciliation implications;
- remote CI status for that SHA;
- owner-run or agent-run evidence completed;
- evidence still required;
- rollback or disable path.

After opening the draft, add the repository's required `DRAFT PR OPEN` comment. Do not merge, activate live trading, or perform release work unless the card and user explicitly authorize it.

## 25. Definition of done for a new feature

A feature is ready for review only when:

- the card was eligible and uncontested;
- the branch and worktree are isolated;
- the diff belongs only to the claimed scope;
- dependencies are merged or the stack is explicit;
- data contracts are typed and documented;
- migrations are tested where applicable;
- missing, stale, timeout, duplicate, unavailable, and refusal paths are covered;
- secrets and account data cannot leak;
- controls preserve authentication and authorization;
- broker-authoritative reconciliation is not weakened;
- local repository gates pass;
- `git diff --check` passes;
- the worktree contains no unrelated changes;
- the draft PR records outstanding exact-head CI and owner-run evidence.

Completion of code does not complete an elapsed soak, provider check, infrastructure drill, or live-activation criterion.

## 26. First actions for the next implementation agent

1. Read `AGENTS.md`, this report, and `docs/ui-design-handoff.md`.
2. Refresh `origin/main` and inspect all worktrees, branches, open PRs, and claims.
3. Do not claim files overlapping PR `#54` while it remains active.
4. Choose one eligible card. If the work is only proposed here, obtain an approved issue first.
5. Confirm dependencies and safety decisions in the issue body.
6. Post the required concurrent-work claim and re-read comments.
7. Create a dedicated issue-numbered branch/worktree.
8. Implement the smallest complete vertical slice, including tests and documentation.
9. Run focused tests, then the full repository gates.
10. Open a draft PR with explicit evidence boundaries.

## 27. Source map

Primary implementation references:

- `AGENTS.md` — repository governance, safety, validation, and claims.
- `docs/ui-design-handoff.md` — UI states, information architecture, and design requirements.
- `docs/crypto-algo-trading-planning-document.md` — phase dependencies and exit criteria.
- `docs/phase-1-gate-acceptance.md` — automated and provider evidence ledger.
- `docs/user-manual.md` — current operator and engineering behavior.
- `api/routes.py`, `api/operator.py`, `api/alerts.py`, and `api/templates/` — operator surface.
- `app/main.py`, `app/trading.py`, `app/recovery.py`, and `app/replay.py` — runtime composition.
- `risk/`, `execution/`, and `portfolio/` — fail-closed trading path.
- `core/models.py` and `db/models.py` — domain and persistence contracts.
- `data/` and `strategy/` — market data, backtest, and replay capabilities.
- `deploy/` and `probes/` — infrastructure and owner-run evidence tooling.

Live records:

- [Issue #51: paper runtime and alerts](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/51)
- [PR #54: draft paper runtime implementation](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/pull/54)
- [Issue #30: explicit unknown cost basis](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/30)
- [Issue #12: 30-day unattended paper soak](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/12)
- [Issue #13: controlled Coinbase live activation](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/13)
- [Issue #14: future broker decision](https://github.com/GHolmesDesigns/algorithmic-crypto-trader/issues/14)

## 28. Final implementation principle

Build new capability only when its source data, authority, failure behavior, and evidence class are explicit. The platform should become more useful without ever becoming more willing to trade through uncertainty.
