import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools" / "cloud_session_setup.sh"
SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh is required to execute the setup script")

# The stub interpreter records calls instead of creating a venv or reaching PyPI.
STUB_PYTHON = """#!/bin/sh
echo "python $*" >> "$STUB_LOG"
case "$*" in
  --version) echo "Python 3.12.3" ;;
  *"pip install"*) exit "${STUB_PIP_EXIT:-0}" ;;
esac
"""

CLEARED = [
    "TRADING_MODE",
    "LIVE_CONFIRMATION",
    "CREDENTIAL_SCOPE",
    "BROKER_PROVIDER",
    "COINBASE_API_KEY",
    "COINBASE_PRIVATE_KEY",
    "GEMINI_API_KEY",
    "GEMINI_API_SECRET",
    "OPERATOR_TOKEN",
    "OPERATOR_ADMIN_TOKEN",
    "ALERT_NTFY_TOKEN",
    "ALERT_SMTP_PASSWORD",
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "stub"\n', encoding="utf-8")
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text(STUB_PYTHON, encoding="utf-8", newline="\n")
    python.chmod(0o755)
    return tmp_path


def run_setup(
    project: Path, *, remote: bool, **extra: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    env_file = project / "claude-env"
    env_file.write_text("", encoding="utf-8")
    log = project / "stub.log"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CLAUDE_CODE_REMOTE", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE"}
    }
    env.update(CLAUDE_PROJECT_DIR=str(project), CLAUDE_ENV_FILE=str(env_file), STUB_LOG=str(log))
    if remote:
        env["CLAUDE_CODE_REMOTE"] = "true"
    env.update(extra)
    result = subprocess.run(
        [SH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return result, env_file, log


def test_session_start_hook_runs_the_setup_only_in_cloud_sessions() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    [entry] = settings["hooks"]["SessionStart"]
    [hook] = entry["hooks"]
    assert entry["matcher"] == "startup|resume"
    assert hook["command"].startswith('if [ "$CLAUDE_CODE_REMOTE" = true ]; then ')
    assert "tools/cloud_session_setup.sh" in hook["command"]
    assert SCRIPT.is_file()


@needs_sh
def test_setup_does_nothing_outside_a_cloud_session(project: Path) -> None:
    result, env_file, log = run_setup(project, remote=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert env_file.read_text(encoding="utf-8") == ""
    assert not log.exists()
    assert not (project / ".venv" / ".pyproject-sha256").exists()


@needs_sh
def test_cloud_setup_clears_trading_variables_and_installs_once(project: Path) -> None:
    result, env_file, log = run_setup(project, remote=True)

    assert result.returncode == 0, result.stderr
    exported = env_file.read_text(encoding="utf-8")
    for name in CLEARED:
        assert f" {name}" in exported
    assert f'export PATH="{project}/.venv/bin:$PATH"' in exported
    assert log.read_text(encoding="utf-8").count("pip install") == 1
    assert f"-e {project}[dev]" in log.read_text(encoding="utf-8")
    assert "Cloud session ready" in result.stdout
    assert "CREDENTIAL_SCOPE=none" in result.stdout

    run_setup(project, remote=True)
    assert log.read_text(encoding="utf-8").count("pip install") == 1

    (project / "pyproject.toml").write_text('[project]\nname = "changed"\n', encoding="utf-8")
    run_setup(project, remote=True)
    assert log.read_text(encoding="utf-8").count("pip install") == 2


@needs_sh
def test_failed_install_still_clears_trading_variables(project: Path) -> None:
    result, env_file, _ = run_setup(project, remote=True, STUB_PIP_EXIT="1")

    assert result.returncode == 1
    assert "installing the project into .venv failed" in result.stderr
    assert "unset COINBASE_API_KEY" in env_file.read_text(encoding="utf-8")
    assert not (project / ".venv" / ".pyproject-sha256").exists()
