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
3. how the change was verified; and
4. which safety, reconciliation, or audit invariants were preserved.
