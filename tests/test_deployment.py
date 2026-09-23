from pathlib import Path

ROOT = Path(__file__).parents[1]


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


def test_backup_and_restore_are_encrypted_off_box_and_redacted() -> None:
    backup = (ROOT / "deploy" / "backup-postgres.sh").read_text()
    restore = (ROOT / "deploy" / "restore-verify-postgres.sh").read_text()

    assert "age --recipient" in backup
    assert "rclone copyto --immutable" in backup
    assert 'rm -f -- "$plain_path"' in backup
    assert "age --decrypt --identity" in restore
    assert "pg_restore --clean --if-exists --exit-on-error" in restore
    assert "to_regclass" in restore
    assert "set -x" not in backup
    assert "set -x" not in restore
