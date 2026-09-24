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

Install Docker Compose, `age`, `rclone`, and the operator-approved rclone remote
on the VPS. Clone the exact reviewed commit into
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
the kill-switch file remains on `kill-switch-data`.

## Startup recovery

Before the HTTP server starts, `app.main` runs startup recovery against the
persisted database state:

1. Orders persisted as `pending_submit` or `unknown` are queried at the broker by
   their persisted `client_order_id` and never resubmitted. Broker state and
   fills are written back to PostgreSQL.
2. Non-terminal orders, their persisted fills, and the latest persisted
   broker-sourced portfolio snapshot (`portfolio_snapshots` batch) are
   reconciled against the broker. The broker result is saved as the new
   snapshot.
3. Any unreadable database, unavailable broker, order the broker has no record
   of, missing portfolio baseline, or divergence trips the kill switch to
   `HALTED`. Only an operator can re-arm it.

The result is shown as "Startup recovery" on the operator dashboard and under
`recovery` in `/operator/state`. The service currently starts without a
broker. In that configuration, a clean database reports `no_broker` and any
pending order halts trading, because it cannot be resolved.

## Nightly encrypted off-box backup

The backup runs on the host and executes `pg_dump` and `psql` inside the `db`
container using that container's own `POSTGRES_USER` and `POSTGRES_DB`. The host
needs Docker Compose, `age`, and `rclone`; it does not need a database URL,
PostgreSQL client tools, or a published database port.

Create `/etc/crypto-trader/backup.env` with mode `0600`. It must define
`BACKUP_REMOTE` and `AGE_RECIPIENT`, and may set `COMPOSE_PROJECT_DIR` (default
`/opt/algorithmic-crypto-trader`) and `BACKUP_DIR` (default
`/var/backups/trader`). The unit runs with `ProtectHome=true`, so rclone reads
`/etc/crypto-trader/rclone.conf` (mode `0600`, never committed) and caches under
`/var/cache/crypto-trader-backup`. `/etc` is read-only to the unit, so use a
key-based remote (for example S3 or B2 keys) rather than an OAuth remote that
must rewrite its token. For the AWS S3 setup, including the least-privilege
policy and `RCLONE_S3_NO_HEAD=true` for versioned buckets, see the
[VPS drill](phase-1.8-vps-drill-checklist.md#one-time-setup).

```sh
install -d -m 0700 /etc/crypto-trader /var/backups/trader
install -m 0600 /root/.config/rclone/rclone.conf /etc/crypto-trader/rclone.conf
install -m 0644 deploy/systemd/crypto-trader-backup.service /etc/systemd/system/
install -m 0644 deploy/systemd/crypto-trader-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crypto-trader-backup.timer
systemctl start crypto-trader-backup.service
journalctl -u crypto-trader-backup.service -n 20 --no-pager
systemctl status crypto-trader-backup.timer --no-pager
```

Each run writes two artifacts: `trader-<UTC>.dump.age`, an encrypted PostgreSQL
custom-format dump, and `trader-<UTC>.manifest.age`, an encrypted list of the
migration version and the row count of every durable state table. Row counts
are taken before and after the dump. If they differ, the run fails without
uploading, and the timer's next run (or a manual start) retries. Temporary
plaintext is deleted in an exit trap, and only the two `.age` files are copied
off-box. The output contains only the two filenames.

## Scratch restore verification

On an isolated operator machine, install `age` and PostgreSQL 16 or newer client
tools. Create a separate, empty scratch database and keep the `age` identity
file there. Never point this command at the production database.

```sh
export AGE_IDENTITY_FILE=/secure/operator/trader-backup-identity.txt
export SCRATCH_DATABASE_URL=postgresql://restore_user:REPLACE_WITH_PASSWORD@127.0.0.1:5433/trader_restore
rclone copyto remote:trader/trader-20260923T021700Z.dump.age /secure/operator/trader-20260923T021700Z.dump.age
rclone copyto remote:trader/trader-20260923T021700Z.manifest.age /secure/operator/trader-20260923T021700Z.manifest.age
sh deploy/restore-verify-postgres.sh   /secure/operator/trader-20260923T021700Z.dump.age   /secure/operator/trader-20260923T021700Z.manifest.age
```

The script restores the dump and then compares the migration version and every
table's row count with the manifest. It succeeds only when it prints
`verified <table>=<rows>` lines followed by `restore_verified=1`. Any
difference prints the mismatched lines and exits non-zero. Record the date,
reviewed commit, artifact names, scratch database identifier, the `verified`
lines, and operator initials in the private operations log. Do not commit that
log or the backup artifacts.

## Restart drill and incident response

1. Confirm the kill-switch state and record the current operator-approved mode.
2. Run `docker compose -f docker-compose.yml -f deploy/docker-compose.vps.yml restart app`.
   Then check `/health`, the operator dashboard's "Startup recovery" line, the
   persisted kill-switch state, and `docker compose ... logs app` for the
   `startup recovery` line, before allowing new entries.
3. For a host restart, use `systemctl reboot` only during the approved window.
   After reconnecting, check Docker, both named volumes, the app health
   endpoint, the startup recovery result, and the latest backup timer result.
4. If startup recovery reports `halted`, leave the kill switch halted, review
   the recorded discrepancies, capture redacted logs, and follow the incident
   owner process. Never retry an ambiguous order without querying the broker by
   its persisted client order ID.

## Evidence boundary

Local tests exercise startup recovery against SQLite and run the backup and
restore scripts against stubbed `docker`, `age`, `rclone`, `psql`, and
`pg_restore` binaries. None of that is operational evidence. A Compose run,
VPS restart drill, and scratch restore are owner-run checks and must be
recorded separately from CI; follow the
[VPS drill checklist](phase-1.8-vps-drill-checklist.md). No provider write or live-trading activation is
part of this card.
