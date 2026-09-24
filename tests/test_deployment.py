import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh is required to execute deploy scripts")

MANIFEST = [
    "alembic_version=0004_portfolio_snapshot_batches",
    "signals=3",
    "orders=2",
    "fills=4",
    "portfolio_snapshots=1",
    "positions_snapshot=1",
    "balances_snapshot=2",
    "equity_curve=1",
    "discrepancies=0",
]

# Stubs stand in for the host tools. They record calls and never touch a real
# database, encryption key, or remote.
STUBS = {
    "docker": """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  *pg_dump*) printf 'PGDMP-plain-dump' ;;
  *psql*)
    count=$(cat "$STUB_DIR/psql-calls" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$STUB_DIR/psql-calls"
    if [ "$count" -gt 1 ] && [ -n "${FAKE_MANIFEST_AFTER:-}" ]; then
      printf '%s\\n' "$FAKE_MANIFEST_AFTER"
    else
      printf '%s\\n' "$FAKE_MANIFEST"
    fi ;;
esac
""",
    "age": """#!/bin/sh
echo "age $*" >> "$STUB_LOG"
decrypt=0; out=""
while [ $# -gt 1 ]; do
  case "$1" in
    --decrypt) decrypt=1; shift ;;
    --output) out=$2; shift 2 ;;
    --recipient|--identity) shift 2 ;;
    *) shift ;;
  esac
done
if [ "$decrypt" = 1 ]; then
  sed '1d' "$1" > "$out"
else
  { echo AGE-ENCRYPTED; cat "$1"; } > "$out"
fi
""",
    "rclone": """#!/bin/sh
echo "rclone $*" >> "$STUB_LOG"
""",
    "pg_restore": """#!/bin/sh
echo "pg_restore $*" >> "$STUB_LOG"
""",
    "psql": """#!/bin/sh
echo "psql $*" >> "$STUB_LOG"
printf '%s\\n' "$FAKE_RESTORED"
""",
}


def stub_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DATABASE_URL", "BACKUP_REMOTE", "AGE_RECIPIENT"}
    }
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        STUB_DIR=str(tmp_path),
        STUB_LOG=str(tmp_path / "calls.log"),
        TMPDIR=str(tmp_path),
        FAKE_MANIFEST="\n".join(reversed(MANIFEST)),
    )
    env.update(extra)
    return env


def run_script(name: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    return subprocess.run(
        [SH, str(ROOT / "deploy" / name), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def backup(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    backup_dir = tmp_path / "backups"
    env = stub_env(
        tmp_path,
        BACKUP_REMOTE="remote:trader/",
        AGE_RECIPIENT="age1examplerecipient",
        BACKUP_DIR=str(backup_dir),
        COMPOSE_PROJECT_DIR=str(tmp_path / "project"),
        **extra,
    )
    return run_script("backup-postgres.sh", env), backup_dir


def test_container_runs_migrations_before_application() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "deploy" / "entrypoint.sh").read_text()

    assert 'ENTRYPOINT ["/usr/local/bin/trading-service-entrypoint"]' in dockerfile
    assert "alembic upgrade head" in entrypoint
    assert 'exec "$@"' in entrypoint
    assert "set -x" not in entrypoint


def test_vps_compose_requires_runtime_secrets_and_persists_state() -> None:
    compose = (ROOT / "deploy" / "docker-compose.vps.yml").read_text()
    base_compose = (ROOT / "docker-compose.yml").read_text()

    assert "POSTGRES_PASSWORD:?" in compose
    assert "DATABASE_URL:?" in compose
    assert "OPERATOR_TOKEN:?" in compose
    assert "KILL_SWITCH_FILE: /var/lib/trader/kill-switch.json" in compose
    assert "kill-switch-data" in base_compose
    assert "postgres-data" in base_compose
    assert "restart: unless-stopped" in base_compose
    assert "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}" in base_compose


def test_backup_unit_gives_rclone_readable_config_under_hardening() -> None:
    unit = (ROOT / "deploy" / "systemd" / "crypto-trader-backup.service").read_text()

    assert "ProtectHome=true" in unit
    assert "Environment=RCLONE_CONFIG=/etc/crypto-trader/rclone.conf" in unit
    assert "CacheDirectory=crypto-trader-backup" in unit
    assert "RCLONE_CACHE_DIR=/var/cache/crypto-trader-backup/" in unit
    assert "ExecStart=/opt/algorithmic-crypto-trader/deploy/backup-postgres.sh" in unit


def test_backup_and_restore_scripts_check_the_same_tables() -> None:
    pattern = re.compile(r"^tables='([^']+)'", re.MULTILINE)
    backup_tables = pattern.search((ROOT / "deploy" / "backup-postgres.sh").read_text())
    restore_tables = pattern.search((ROOT / "deploy" / "restore-verify-postgres.sh").read_text())

    assert backup_tables is not None and restore_tables is not None
    assert backup_tables.group(1) == restore_tables.group(1)
    assert "portfolio_snapshots" in backup_tables.group(1).split()
    for script in ("backup-postgres.sh", "restore-verify-postgres.sh"):
        assert "set -x" not in (ROOT / "deploy" / script).read_text()


@needs_sh
def test_backup_dumps_inside_db_container_and_ships_only_encrypted_artifacts(tmp_path) -> None:
    result, backup_dir = backup(tmp_path)

    assert result.returncode == 0, result.stderr
    names = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert names["encrypted_backup"].endswith(".dump.age")
    assert names["encrypted_manifest"].endswith(".manifest.age")
    assert sorted(path.name for path in backup_dir.iterdir()) == sorted(names.values())
    for path in backup_dir.iterdir():
        assert path.read_text().startswith("AGE-ENCRYPTED")

    calls = (tmp_path / "calls.log").read_text()
    assert "exec -T db sh -c pg_dump" in calls
    # Credentials come from the container environment; no connection URL leaves the host.
    assert '--username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' in calls
    assert "postgresql" not in calls
    shipped = [line for line in calls.splitlines() if line.startswith("rclone")]
    assert shipped == [
        f"rclone copyto --immutable {backup_dir}/{name} remote:trader/{name}"
        for name in (names["encrypted_backup"], names["encrypted_manifest"])
    ]


@needs_sh
def test_backup_fails_without_upload_when_rows_change_during_dump(tmp_path) -> None:
    changed = "\n".join(line.replace("orders=2", "orders=3") for line in MANIFEST)

    result, backup_dir = backup(tmp_path, FAKE_MANIFEST_AFTER=changed)

    assert result.returncode != 0
    assert "row counts changed" in result.stderr
    assert "rclone" not in (tmp_path / "calls.log").read_text()
    assert list(backup_dir.iterdir()) == []


@needs_sh
def test_backup_refuses_to_run_without_encryption_recipient(tmp_path) -> None:
    env = stub_env(tmp_path, BACKUP_REMOTE="remote:trader", BACKUP_DIR=str(tmp_path / "b"))

    result = run_script("backup-postgres.sh", env)

    assert result.returncode != 0
    assert "AGE_RECIPIENT" in result.stderr
    assert not (tmp_path / "calls.log").exists()


def encrypted_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    result, backup_dir = backup(tmp_path)
    assert result.returncode == 0, result.stderr
    (tmp_path / "calls.log").unlink()
    dump = next(backup_dir.glob("*.dump.age"))
    manifest = next(backup_dir.glob("*.manifest.age"))
    return dump, manifest


def restore(tmp_path: Path, restored: list[str]) -> subprocess.CompletedProcess[str]:
    dump, manifest = encrypted_artifacts(tmp_path)
    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    env = stub_env(
        restore_dir,
        AGE_IDENTITY_FILE=str(tmp_path / "identity.txt"),
        SCRATCH_DATABASE_URL="postgresql://restore@127.0.0.1:5433/trader_restore",
        FAKE_RESTORED="\n".join(restored),
    )
    return run_script("restore-verify-postgres.sh", env, str(dump), str(manifest))


@needs_sh
def test_restore_verifies_row_counts_against_backup_manifest(tmp_path) -> None:
    result = restore(tmp_path, MANIFEST)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == "restore_verified=1"
    assert "verified orders=2" in lines
    assert "verified alembic_version=0004_portfolio_snapshot_batches" in lines
    assert "restore@" not in result.stdout + result.stderr
    calls = (tmp_path / "restore" / "calls.log").read_text()
    assert "pg_restore --clean --if-exists --exit-on-error --no-owner" in calls
    leftovers = [path for path in (tmp_path / "restore").iterdir() if "trader-restore" in path.name]
    assert leftovers == []


@needs_sh
@pytest.mark.parametrize(
    "restored",
    [
        [line.replace("fills=4", "fills=3") for line in MANIFEST],
        [line for line in MANIFEST if not line.startswith("discrepancies")],
        [line.replace("0004_portfolio", "0003_portfolio") for line in MANIFEST],
    ],
    ids=["row-count", "missing-table", "migration-version"],
)
def test_restore_rejects_contents_that_differ_from_manifest(tmp_path, restored) -> None:
    result = restore(tmp_path, restored)

    assert result.returncode != 0
    assert "restore_verified" not in result.stdout
    assert "do not match" in result.stderr


DRILL_DOCKER_STUB = """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
state_file="$STUB_DIR/kill-switch"
[ -f "$state_file" ] || echo running > "$state_file"
case "$1" in
  inspect) echo healthy; exit 0 ;;
  volume) printf 'trader_postgres-data\ntrader_kill-switch-data\n'; exit 0 ;;
esac
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --project-directory|-f|--env-file) shift 2 ;;
    *) break ;;
  esac
done
case "$1" in
  ps) echo container-id ;;
  logs) echo '{"message": "startup recovery no_broker: clean (kill switch running)"}' ;;
  exec)
    if [ "$3" = db ]; then cat "$STUB_DIR/counts.txt"; exit 0; fi
    case "$7 $8" in
      "GET /operator/kill-switch") cat "$state_file" ;;
      "POST /operator/pause") echo paused > "$state_file"; echo paused ;;
      "POST /operator/rearm")
        echo "$9" > "$STUB_DIR/rearm-role"; echo running > "$state_file"; echo running ;;
      "GET /operator/state") echo "${FAKE_RECOVERY:-no_broker}" ;;
    esac ;;
esac
"""


def drill_env(
    tmp_path: Path, mode: str = "paper", scope: str = "none", **extra: str
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(DRILL_DOCKER_STUB, encoding="utf-8", newline="\n")
    docker.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        f"TRADING_MODE={mode}\nCREDENTIAL_SCOPE={scope}\nOPERATOR_TOKEN=never-printed\n",
        encoding="utf-8",
    )
    (tmp_path / "counts.txt").write_text("\n".join(MANIFEST) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        STUB_DIR=str(tmp_path),
        STUB_LOG=str(tmp_path / "calls.log"),
        COMPOSE_PROJECT_DIR=str(project),
        DRILL_STATE_DIR=str(tmp_path / "state"),
    )
    env.update(extra)
    return env


def test_drill_api_helper_is_valid_python() -> None:
    script = (ROOT / "deploy" / "drill.sh").read_text()
    code = script.split("python -c '", 1)[1].split('\' "$@"', 1)[0]

    compile(code, "drill-api", "exec")
    assert "OPERATOR_ADMIN_TOKEN" in code
    assert "print(token" not in code


@needs_sh
def test_drill_restart_and_reboot_phases_pass_and_restore_kill_switch(tmp_path) -> None:
    env = drill_env(tmp_path)

    before = run_script("drill.sh", env, "before-reboot")
    after = run_script("drill.sh", env, "after-reboot")

    assert before.returncode == 0, before.stdout + before.stderr
    assert "RESULT before-reboot: PASS" in before.stdout
    assert "CHECK app-restart-kill-switch: PASS state=paused" in before.stdout
    assert after.returncode == 0, after.stdout + after.stderr
    assert "CHECK reboot-rows: PASS row counts unchanged" in after.stdout
    assert "INFO kill switch left running (was running before the drill)" in after.stdout
    assert (tmp_path / "rearm-role").read_text().strip() == "admin"
    assert "never-printed" not in before.stdout + after.stdout + before.stderr + after.stderr


@needs_sh
def test_drill_fails_and_stays_paused_when_rows_change_across_reboot(tmp_path) -> None:
    env = drill_env(tmp_path)
    assert run_script("drill.sh", env, "before-reboot").returncode == 0
    counts = tmp_path / "counts.txt"
    counts.write_text(counts.read_text().replace("orders=2", "orders=1"), encoding="utf-8")

    after = run_script("drill.sh", env, "after-reboot")

    assert after.returncode != 0
    assert "CHECK reboot-rows: FAIL" in after.stdout
    assert "RESULT after-reboot: FAIL (1 failed checks)" in after.stdout
    assert "INFO kill switch left paused" in after.stdout
    assert not (tmp_path / "rearm-role").exists()


@needs_sh
def test_drill_fails_when_startup_recovery_halted(tmp_path) -> None:
    env = drill_env(tmp_path, FAKE_RECOVERY="halted")

    result = run_script("drill.sh", env, "before-reboot")

    assert result.returncode != 0
    assert "CHECK app-restart-recovery: FAIL status=halted" in result.stdout


@needs_sh
@pytest.mark.parametrize(("mode", "scope"), [("live", "none"), ("paper", "trade")])
def test_drill_refuses_live_or_trade_capable_configuration(tmp_path, mode, scope) -> None:
    env = drill_env(tmp_path, mode=mode, scope=scope)

    result = run_script("drill.sh", env, "before-reboot")

    assert result.returncode == 2
    assert "refusing" in result.stderr
    assert not (tmp_path / "calls.log").exists()


def test_restore_drill_workflow_is_manual_or_scheduled_and_keeps_secrets_out_of_logs() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "restore-drill.yml").read_text())
    triggers = workflow[True]
    job = workflow["jobs"]["restore"]
    script = "\n".join(step.get("run", "") for step in job["steps"])

    assert set(triggers) == {"schedule", "workflow_dispatch", "pull_request"}
    assert triggers["pull_request"] == {"types": ["labeled"]}
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "sh deploy/restore-verify-postgres.sh" in script
    assert "set -x" not in script
    assert "${{ secrets." not in script
    assert job["steps"][-1]["if"] == "always()"


def test_vps_bootstrap_generates_paper_only_env_without_printing_secrets() -> None:
    script = (ROOT / "deploy" / "bootstrap-vps.sh").read_text()
    printed = "\n".join(line for line in script.splitlines() if "printf" in line or "echo" in line)

    assert "TRADING_MODE=paper" in script
    assert "CREDENTIAL_SCOPE=none" in script
    assert 'if [ ! -f "$env_file" ]; then' in script
    assert 'chmod 0600 "$env_file"' in script
    assert "set -x" not in script
    for secret in ("db_password", "OPERATOR_TOKEN", "OPERATOR_ADMIN_TOKEN", "openssl rand"):
        assert secret not in printed


@needs_sh
def test_drill_status_reports_kill_switch_and_recovery(tmp_path) -> None:
    env = drill_env(tmp_path, FAKE_RECOVERY="halted")

    result = run_script("drill.sh", env, "status")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["kill_switch=running", "recovery=halted"]


def test_drill_backup_phase_fails_on_any_error_in_the_run_log() -> None:
    script = (ROOT / "deploy" / "drill.sh").read_text()

    assert "grep -cE 'ERROR|Forbidden|AccessDenied'" in script
    assert "check backup-log-clean" in script


def test_restart_rehearsal_is_manual_uses_no_secrets_and_proves_fail_closed_restart() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "restart-rehearsal.yml").read_text()
    )
    job = workflow["jobs"]["rehearsal"]
    script = "\n".join(step.get("run", "") for step in job["steps"])

    assert set(workflow[True]) == {"workflow_dispatch", "pull_request"}
    assert workflow[True]["pull_request"] == {"types": ["labeled"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in (ROOT / ".github" / "workflows" / "restart-rehearsal.yml").read_text()
    assert "TRADING_MODE=paper" in script
    assert "sudo systemctl restart docker" in script
    assert "'pending_submit'" in script
    assert "grep -qx 'kill_switch=halted'" in script
    assert "grep -qx 'restore_verified=1'" in script
    assert job["steps"][-1]["if"] == "always()"
