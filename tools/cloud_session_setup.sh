#!/bin/sh

# SessionStart setup for Claude Code cloud sessions (Issue #68). A cloud
# session is a fresh Ubuntu VM with a new clone, so this script:
#
#   1. clears trading, credential, operator-token, and alert variables for the
#      session's commands, so it runs on the backtest/no-credential defaults CI
#      uses even if one was added to the cloud environment by mistake;
#   2. installs the project and its dev tools into .venv, again only when
#      pyproject.toml changes, and puts .venv/bin first on PATH.
#
# It does nothing outside the cloud: CLAUDE_CODE_REMOTE is "true" only there.
# See docs/cloud-sessions.md.
set -eu

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

project_dir=${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
venv="$project_dir/.venv"
stamp="$venv/.pyproject-sha256"

# Safety first, so a failed install below still leaves the variables cleared.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  cat >> "$CLAUDE_ENV_FILE" <<EOF
unset TRADING_MODE LIVE_CONFIRMATION CREDENTIAL_SCOPE BROKER_PROVIDER PAPER_RUNTIME_ENABLED
unset COINBASE_API_KEY COINBASE_PRIVATE_KEY GEMINI_API_KEY GEMINI_API_SECRET
unset OPERATOR_TOKEN OPERATOR_ADMIN_TOKEN
unset ALERT_NTFY_TOPIC_URL ALERT_NTFY_TOKEN ALERT_SMTP_HOST ALERT_SMTP_USERNAME ALERT_SMTP_PASSWORD
export VIRTUAL_ENV="$venv"
export PATH="$venv/bin:\$PATH"
EOF
fi

find_python() {
  for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if [ ! -x "$venv/bin/python" ]; then
  if ! python=$(find_python); then
    echo "cloud session setup: Python 3.12 or newer was not found" >&2
    exit 1
  fi
  "$python" -m venv "$venv"
fi

wanted=$(sha256sum "$project_dir/pyproject.toml" | cut -d ' ' -f 1)
if [ "$(cat "$stamp" 2>/dev/null || true)" != "$wanted" ]; then
  if ! "$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    -e "$project_dir[dev]" >&2; then
    echo "cloud session setup: installing the project into .venv failed" >&2
    exit 1
  fi
  echo "$wanted" > "$stamp"
fi

# Plain stdout from a SessionStart hook becomes context for the session.
echo "Cloud session ready: project and dev tools installed in .venv ($("$venv/bin/python" --version))." \
  "Trading, credential, operator-token, and alert variables are cleared, so the app defaults to" \
  "backtest with CREDENTIAL_SCOPE=none. Do not contact exchanges from a cloud session;" \
  "see docs/cloud-sessions.md."
