# Contributing

## Before opening a pull request

- Keep changes small and explain the safety or operational impact.
- Run the relevant local formatting, lint, type-check, and test commands when they exist.
- Do not use production exchange credentials or live account data in development or CI.
- Prefer deterministic fixtures, `SimulatedBroker`, and Gemini Sandbox for automated validation.
- Expensive integration, replay, soak, and live-provider checks should be manual or scheduled, not added to every pull request by default.

## Pull requests

Describe:

1. what changed;
2. which execution modes are affected;
3. how the change was verified;
4. which safety, reconciliation, or audit invariants were preserved; and
5. the application version change, old to new, or `no bump` and why (see Versioning).

## Versioning

The application version is `version` under `[project]` in `pyproject.toml`. It is the only place the
number is written. The service reads it at runtime and shows it as **Application version** on the
dashboard's System health panel and in `/health/detail`, and on the startup banner. `/health` does not show it.

The format is `MAJOR.MINOR.PATCH`: digits only, no prefix or suffix. MAJOR stays 0 until the owner
decides the first stable release; an agent never raises it.

**Raise it in the pull request that changes what ships.** That means anything under `app/`, `api/`,
`brokers/`, `core/`, `data/`, `db/`, `execution/`, `portfolio/`, `risk/`, `strategy/` or `alembic/`,
plus `alembic.ini`, the `Dockerfile`, `deploy/entrypoint.sh`, and the runtime `dependencies` list in
`pyproject.toml`.

- **MINOR** for a new operator-visible capability, page or endpoint; a database migration; a new
  broker, venue or mode path; or any change to a safety gate, refusal path or contract. PATCH resets to 0.
- **PATCH** for a bug fix, a reliability or internal change, or a dependency update with no behavior change.
- **No bump** for documentation, tests, CI workflows, the dev compose stack, and anything else that is not in
  the image.
- When two descriptions apply, take the higher one.

Mechanics:

- Raise it in the same pull request, after refreshing from `origin/main`, starting from the version `main`
  has now.
- If two open branches pick the same number, whoever merges second rebases and takes the next one. A card
  claim does not reserve a number.
- The pull-request description states the old and new version, or `no bump` with the reason.
- `Repository preflight` runs `tools/check_version_bump.py` on every pull request. It fails a version that is
  malformed or lower than the base branch's, and an unchanged version when the diff touches a shipped path or
  the runtime dependency list. It also fails if it cannot read the base branch. It cannot tell MINOR from
  PATCH; the reviewer checks that. To run it yourself after committing: `python tools/check_version_bump.py --base origin/main`.
- A version is not a release. This repository does not tag, publish or release from it; that needs explicit
  authorization from the owner.
