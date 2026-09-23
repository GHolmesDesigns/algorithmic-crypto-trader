# Phase 1.8 deployment, backups, and restore

This runbook deploys the service with Docker Compose, keeps the PostgreSQL and
kill-switch state on named volumes, and makes database backups useful only when
the encrypted artifact has been restored into a separate scratch database.

## Safety boundary

- Use a dedicated VPS and a static egress IP. Add that IP to an exchange allowlist
  only for the credential scope approved for the selected mode.
- Keep `TRADING_MODE=paper` and `CREDENTIAL_SCOPE=none` until the separate live
  activation card is accepted. This card does not authorize live trading.
- Store the production `.env` and the backup environment file outside Git with
  mode `0600`. The repository contains no credentials or database dumps.
- Do not use `docker compose down -v`; named volumes contain the recovery state.
- The app container runs `alembic upgrade head` before the application process and
  exits if the database URL is absent or the migration fails.

## VPS deployment

Install Docker Compose, `age`, `rclone`, PostgreSQL client tools, and the
operator-approved rclone remote on the VPS. Clone the exact reviewed commit into
`/opt/algorithmic-crypto-trader`, then create `/opt/algorithmic-crypto-trader/.env`
with the values below. Generate every `REPLACE_*` value locally; do not paste
credentials into a shell history, issue, or log.

```text
POSTGRES_DB=trader
POSTGRES_USER=trader
POSTGRES_PASSWORD=REPLACE_WITH_RANDOM_DATABASE_PASSWORD
DATABASE_URL=postgresql+psycopg://trader:REPLACE_WITH_URL_ESCAPED_PASSWORD@db:5432/trader
TRADING_MODE=paper
CREDENTIAL_SCOPE=none
OPERATOR_TOKEN=REPLACE_WITH_RANDOM_OPERATOR_TOKEN
OPERATOR_ADMIN_TOKEN=REPLACE_WITH_RANDOM_ADMIN_TOKEN
```

Start and check the deployment:

```sh
cd /opt/algorithmic-crypto-trader
docker compose -f docker-compose.yml -f deploy/docker-compose.vps.yml --env-file .env up -d --build
docker compose -f docker-compose.yml -f deploy/docker-compose.vps.yml ps
curl --fail http://127.0.0.1:8000/health
```

The expected recovery properties are: the PostgreSQL container keeps its named
volume, the app waits for PostgreSQL health, migrations run before startup, and
the kill-switch file remains on `kill-switch-data`. Order rows and portfolio
snapshots are durable PostgreSQL records; on a later process start the existing
execution/reconciliation components can query them and reconcile broker state
before new entries.

## Nightly encrypted off-box backup

Create `/etc/crypto-trader/backup.env` with mode `0600`. It must define
`DATABASE_URL`, `BACKUP_REMOTE`, and an `AGE_RECIPIENT`; rclone credentials stay
in the host's protected rclone configuration and are never committed here.

```sh
install -m 0755 deploy/backup-postgres.sh /usr/local/bin/crypto-trader-backup
install -m 0644 deploy/systemd/crypto-trader-backup.service /etc/systemd/system/
install -m 0644 deploy/systemd/crypto-trader-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crypto-trader-backup.timer
systemctl start crypto-trader-backup.service
systemctl status crypto-trader-backup.timer --no-pager
```

The backup script creates a PostgreSQL custom-format dump, encrypts it with
`age`, deletes the temporary plaintext dump in a `finally`-equivalent trap, and
copies only the `.dump.age` artifact to the off-box rclone destination. Its
output contains only the generated filename.

## Scratch restore verification

Provision a separate empty PostgreSQL database and an `age` identity file on an
isolated operator machine. Never point this command at the production database.

```sh
export AGE_IDENTITY_FILE=/secure/operator/trader-backup-identity.txt
export SCRATCH_DATABASE_URL=postgresql://restore_user:REPLACE_WITH_PASSWORD@127.0.0.1:5433/trader_restore
install -m 0755 deploy/restore-verify-postgres.sh /usr/local/bin/crypto-trader-restore-verify
crypto-trader-restore-verify /secure/operator/trader-20260923T021700Z.dump.age
```

The command is successful only when `restore_verified=1` is printed after
`pg_restore` completes and all migration/state tables are present. Record the
date, reviewed commit, artifact name, scratch database identifier, table checks,
and operator initials in the private operations log. Do not commit that log or
the backup artifact.

## Restart drill and incident response

1. Confirm the kill-switch state and record the current operator-approved mode.
2. Run `docker compose ... restart app`, then check `/health`, the operator
   dashboard, persisted kill-switch state, pending order recovery, and the latest
   broker-authoritative portfolio reconciliation before allowing new entries.
3. For a host restart, use `systemctl reboot` only during the approved window;
   after reconnecting, check Docker, both named volumes, the app health endpoint,
   and the latest backup timer result.
4. If recovery or reconciliation is not correct, leave the kill switch halted,
   capture redacted logs, and follow the incident owner process. Never retry an
   ambiguous order without querying the broker by its persisted client order ID.

## Evidence boundary

Local tests validate the configuration and fail-closed script contracts. A
successful local Compose run, VPS restart drill, and scratch restore are
owner-run operational evidence and must be recorded separately from CI. No
provider write or live-trading activation is part of this card.
