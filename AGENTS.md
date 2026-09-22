# Agent Guide

This guide governs agents and contributors working concurrently in this repository. The repository is public, `main` is protected, and the system can eventually place real orders. Preserve isolation, auditability, and fail-closed behavior while work is in progress.

## Repository shape

The planned Python service is organized by responsibility:

- `core/`: shared domain models, modes, identifiers, and errors.
- `brokers/`: `BrokerInterface`, capabilities, `SimulatedBroker`, `GeminiBroker`, and `CoinbaseBroker`.
- `data/`: historical candles, WebSocket ingestion, normalization, validation, and replay recording.
- `strategy/`: strategy code that receives market state and returns signals; it does not import broker, database, HTTP, or network code.
- `risk/`: independent risk gates and `RiskApproval`.
- `execution/`: order persistence, submission, ambiguity resolution, and fill handling; it accepts `RiskApproval`, never a raw signal.
- `portfolio/`: positions, balances, equity, and reconciliation.
- `api/`: authenticated operator controls, health, status, and alerts.
- `tests/`: unit, contract, integration, and controlled end-to-end tests.
- `docs/`: capability evidence, runbooks, decisions, and validation records.

Keep provider-specific behavior inside `brokers/`. Strategy, risk, portfolio, and reconciliation code must not be rewritten to accommodate one venue.

## Before claiming work

An agent must inspect the current repository and the full card before editing:

```text
git status --short --branch
git worktree list
git branch -a
gh issue view <number> --repo GHolmesDesigns/algorithmic-crypto-trader
gh pr list --repo GHolmesDesigns/algorithmic-crypto-trader --state open
```

Read the issue body, dependencies, comments, existing claims, related pull requests, and any active branch or worktree. An empty pull-request list does not prove that a card is unowned.

Do not claim a card that is blocked, dependency-incomplete, materially overlaps active work, or requires an unstated product or safety decision.

## Concurrent work protocol

One agent owns one card at a time. Claim the card in a comment before implementation using this form:

```text
CONCURRENT WORK CLAIM
Agent: <agent or platform>
Branch: <type>/<issue>-<slug>
UTC: <timestamp>
Scope: <files or behavior covered>
Dependencies: <completed cards or none>
```

Re-read the comments after claiming. If another valid claim already exists, stop work on that card and choose another eligible card. The earliest complete claim with a valid branch, timestamp, scope, and dependency check wins.

Parallel preparation is allowed when it produces independent research, fixtures, or documentation. Implementation may proceed concurrently only when the cards have no material file or contract overlap. A dependent implementation waits for its predecessor or uses an explicitly stacked branch and names that relationship in the pull request.

## Worktree and branch isolation

- Keep the shared checkout and `main` untouched during implementation.
- Refresh from `origin/main` before creating a branch or worktree.
- Use a dedicated worktree for each active card. Never reuse another agent's worktree.
- Name branches `<type>/<issue>-<short-slug>` using `feat`, `fix`, `chore`, or `docs`.
- Keep the issue number in the branch name; do not use a version number as the branch identifier.
- Preserve unrelated files, branches, worktrees, stashes, and untracked artifacts.
- Never use `git reset --hard`, broad cleanup, or `git add -A` to solve a local problem.
- Stage only the paths belonging to the claimed card.

Before opening a pull request, rebase or merge the current `origin/main` according to the repository policy, resolve conflicts in the isolated worktree, and verify the final diff still belongs to the card.

## Trading and credential boundaries

- Never commit API keys, private keys, tokens, `.env` files, account credentials, production account identifiers, database dumps, or live operational logs.
- Automated tests use deterministic fixtures, mocks, `SimulatedBroker`, or Gemini Sandbox. CI must not place real orders or contact a production exchange.
- Coinbase production access is owner-run and must use the separately managed production credential with transfers and withdrawals disabled.
- A live action requires the configured live mode, matching credential scope, explicit confirmation, an active risk approval, and an auditable event.
- `GeminiBroker` must reject non-sandbox hosts at construction. Do not make the sandbox host a freely configurable production endpoint.
- An ambiguous submission is `UNKNOWN`. Query the broker by the persisted `client_order_id` before any retry.
- The broker is authoritative during reconciliation. A divergence creates an event, alerts the operator, and applies the configured safety response before new entries.
- Redact secrets from logs, fixtures, issue comments, pull requests, and test output.

## Design invariants

- Use `Decimal` for prices, quantities, fees, balances, and P/L.
- Keep `TRADING_MODE` values exactly `backtest`, `replay`, `paper`, and `live`.
- Strategy code returns signals but cannot submit orders or reach external systems.
- Execution accepts only a `RiskApproval`.
- Every order links to its signal, strategy version, risk decision, and fills.
- Persist the idempotency key before submission and enforce uniqueness in the database.
- Missing or stale risk inputs fail closed.
- The kill switch has persistent `RUNNING`, `PAUSED`, and `HALTED` states and remains available through the authenticated API, operator surface, and file/environment path.
- Preview or planning code must not perform provider writes. Any owner-run provider probe must name exact targets, use a bounded request budget, clean up in `finally`, and record a redacted result.

## Validation and evidence

Report validation in three separate categories:

1. **Local validation:** commands run in the isolated worktree and their results.
2. **Remote CI:** completed checks for the exact pull-request head SHA.
3. **Owner-run verification:** manual sandbox or production checks that require credentials or real provider access.

A mock, skipped check, or successful plan-only run is not evidence of live-provider behavior. Do not claim a provider capability until the dated result and source are recorded in `docs/`.

The repository currently has a lightweight `Repository preflight` workflow. Add expensive replay, integration, soak, or provider checks as manual or scheduled work unless the card explicitly requires a protected check.

## Pull-request handoff

Open a draft pull request after the claimed scope is implemented and locally checked. Include:

- the issue and dependency chain;
- the exact behavior changed;
- files and contracts affected;
- local validation and its results;
- safety and reconciliation implications;
- whether any owner-run provider verification remains;
- the final commit SHA.

Then add a comment in this form:

```text
DRAFT PR OPEN
Branch: <branch>
PR: <url>
Commit: <sha>
Validation: <summary>
Owner-run verification: <complete or still required>
```

Do not merge, enable live trading, or perform release work unless the card and the user explicitly authorize that step. A pull request is not green until the required checks have completed for its exact final head.

## Definition of done

A card is ready for review when:

- the claim and branch are recorded;
- the implementation is limited to the claimed scope;
- focused tests cover the intended user or domain outcome and the plausible regression;
- failure, stale-input, timeout, duplicate, and refusal paths are covered where relevant;
- `git diff --check` passes;
- no secret or real-provider side effect was introduced;
- the worktree is clean except for committed work;
- the draft pull request records what remains unverified.

Never lower a safety gate, remove a required check, or weaken a refusal path merely to obtain a green result. If a check is flaky or blocked by infrastructure, record the cause and preserve the fail-closed behavior.
