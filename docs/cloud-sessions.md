# Claude Code cloud sessions

Issue #68. A cloud session runs Claude Code on a fresh Ubuntu 24.04 VM that Anthropic hosts, with a new clone of this repository. You start one from [claude.ai/code](https://claude.ai/code), from the Claude mobile app, or by picking **Cloud** instead of **Local** in the desktop app. It keeps working after you close your laptop.

## What the repository sets up

`.claude/settings.json` runs `tools/cloud_session_setup.sh` whenever a cloud session starts or resumes. The script:

- clears `TRADING_MODE`, `CREDENTIAL_SCOPE`, `LIVE_CONFIRMATION`, `BROKER_PROVIDER`, the Coinbase and Gemini keys, the operator tokens, and the alert credentials for every command the session runs, so the app falls back to `backtest` with `CREDENTIAL_SCOPE=none`, the same as CI;
- installs the project and its dev tools (`pip install -e ".[dev]"`) into `.venv` and puts `.venv/bin` first on `PATH`, so `pytest`, `ruff`, and `mypy` match `Repository preflight`;
- reinstalls only when `pyproject.toml` changes.

The hook checks `CLAUDE_CODE_REMOTE`, which is `true` only in the cloud, so local and desktop sessions skip it.

## Environment settings on claude.ai

Environments are set on your Claude account, not in the repository. Open the environment selector (the cloud icon above the message box at claude.ai/code, or in the desktop app's prompt box with **Cloud** selected) and add or edit an environment:

| Field | Value |
| :-- | :-- |
| Name | `crypto-trader` (any name works) |
| Network access | **Trusted**. Never **Full**. |
| Environment variables | Leave empty. |
| Setup script | Leave empty. The repository's hook does the install. |

An unmodified **Default** environment already matches this table, so it works as-is.

Never put exchange keys, operator tokens, backup keys, or AWS credentials in the environment. Anyone who uses the environment can read its variables, and a cloud session has no use for them.

Cloud sessions push branches and open pull requests through the Claude GitHub App, so install it on this repository if it isn't already.

## What a cloud session cannot do

- **Reach an exchange.** Coinbase and Gemini hosts are not on the Trusted network allowlist, so exchange checks, sandbox probes, and anything else needing provider access stay owner-run, as `AGENTS.md` requires.
- **Reach the paper VPS.** The agent SSH key lives only on the owner's computer, and the VPS firewall limits SSH to known sources. Deploys, restart drills, and backups run from a local session.
- **Place orders.** The cleared variables keep the app in `backtest` with no credential scope.

Everything else in `AGENTS.md` still applies: claim the card, keep one card per branch, and open a draft pull request with the validation categories.
