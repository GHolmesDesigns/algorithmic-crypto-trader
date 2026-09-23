# Phase 1.8 VPS drill checklist

Owner-run checklist for the Issue #10 acceptance evidence: Compose startup,
restart and reboot state recovery, encrypted backup creation, and scratch
restore verification. It complements the
[deployment, backups, and restore runbook](phase-1.8-deployment-backups-restore.md).

Every command runs **on the VPS** as root in `/opt/algorithmic-crypto-trader`
unless a step says otherwise. Keep `TRADING_MODE=paper` and
`CREDENTIAL_SCOPE=none` throughout. Record results in the evidence template at
the end.

## 0. Before you start

- [ ] Pick a maintenance window. This is a reboot of a paper deployment, with
      no live trading.
- [ ] Make sure you have a separate operator machine with `age`, PostgreSQL 16+
      client tools and `rclone`, plus somewhere to run an empty scratch
      database. You need this for phase 5.
- [ ] Create a scratch folder and put a helper and an auth header in it. The
      header keeps the operator token out of shell history.

```sh
mkdir -p -m 0700 /root/drill && cd /opt/algorithmic-crypto-trader
dc() { docker compose -f docker-compose.yml -f deploy/docker-compose.vps.yml --env-file .env "$@"; }
printf 'X-Operator-Token: %s\n' "$(grep ^OPERATOR_TOKEN= .env | cut -d= -f2-)" > /root/drill/op-header && chmod 0600 /root/drill/op-header
```

- [ ] Save the row-count query. It's the same query the backup manifest uses.

```sh
cat > /root/drill/counts.sql <<'SQL'
SELECT 'alembic_version=' || version_num FROM alembic_version
UNION ALL SELECT 'signals=' || count(*) FROM signals
UNION ALL SELECT 'orders=' || count(*) FROM orders
UNION ALL SELECT 'fills=' || count(*) FROM fills
UNION ALL SELECT 'portfolio_snapshots=' || count(*) FROM portfolio_snapshots
UNION ALL SELECT 'positions_snapshot=' || count(*) FROM positions_snapshot
UNION ALL SELECT 'balances_snapshot=' || count(*) FROM balances_snapshot
UNION ALL SELECT 'equity_curve=' || count(*) FROM equity_curve
UNION ALL SELECT 'discrepancies=' || count(*) FROM discrepancies;
SQL
counts() { dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1' < /root/drill/counts.sql | LC_ALL=C sort; }
```

The `dc` and `counts` helpers only last for the current shell. Define them
again after the reboot in phase 3.

## 1. Deploy the reviewed commit

- [ ] Check out the reviewed commit and rebuild:

```sh
git fetch origin && git checkout --detach <reviewed-commit-sha>
dc up -d --build
dc ps
```

- [ ] Both `db` and `app` show `healthy`.
- [ ] Confirm the migration ran and startup recovery logged a result:

```sh
dc logs app | grep -E "0003_risk_execution_portfolio -> 0004|startup recovery"
```

- [ ] **Expected:** `startup recovery no_broker: … (kill switch running)`. No
      broker is connected yet, so recovery only checks for pending orders. If
      you get `halted`, **stop here**: pending orders exist that can't be
      resolved. Record the detail line.
- [ ] Check health and the recovery status:

```sh
curl -fsS http://127.0.0.1:8000/health
curl -fsS -H @/root/drill/op-header http://127.0.0.1:8000/operator/state | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["recovery"], d["risk"])'
```

- [ ] The operator dashboard shows a "Startup recovery" line.

## 2. App restart drill

- [ ] Pause trading on purpose. The paused state is the marker that shows the
      kill switch survived the restart. Then record the baseline row counts:

```sh
curl -fsS -X POST -H @/root/drill/op-header http://127.0.0.1:8000/operator/pause
counts > /root/drill/before.txt && cat /root/drill/before.txt
```

- [ ] Restart the app:

```sh
dc restart app
dc ps
```

- [ ] Verify what came back:

```sh
curl -fsS -H @/root/drill/op-header http://127.0.0.1:8000/operator/kill-switch
dc logs --since 5m app | grep "startup recovery"
counts > /root/drill/after-app-restart.txt && diff /root/drill/before.txt /root/drill/after-app-restart.txt && echo ROWS-MATCH
```

- [ ] **Pass criteria:**
  - the kill switch still reads `paused`;
  - the recovery line is present with the same status as in phase 1;
  - `ROWS-MATCH` is printed.

## 3. Host reboot drill

- [ ] Note the most recent backup-timer result first (skip this if phase 4 isn't
      set up yet), then reboot:

```sh
systemctl reboot
```

- [ ] After reconnecting, define the `dc` and `counts` helpers again (phase 0),
      then check:

```sh
cd /opt/algorithmic-crypto-trader && dc ps
docker volume ls | grep -E "postgres-data|kill-switch-data"
curl -fsS http://127.0.0.1:8000/health
curl -fsS -H @/root/drill/op-header http://127.0.0.1:8000/operator/kill-switch
dc logs app | grep "startup recovery" | tail -1
counts > /root/drill/after-reboot.txt && diff /root/drill/before.txt /root/drill/after-reboot.txt && echo ROWS-MATCH
```

- [ ] **Pass criteria:**
  - both containers came back on their own (`restart: unless-stopped`);
  - both named volumes are listed;
  - the kill switch is still `paused`;
  - the recovery line shows the same status as before;
  - `ROWS-MATCH` is printed.
- [ ] Re-arm through the operator dashboard with the **admin** token. Don't
      paste the admin token into a terminal.

## 4. Encrypted backup

- [ ] Set up the config. `/etc/crypto-trader/backup.env` (mode `0600`) needs
      only `BACKUP_REMOTE` and `AGE_RECIPIENT`, the **public** `age1…` key. The
      private identity file must never be on the VPS. Use a key-based rclone
      remote such as S3 or B2 keys.

```sh
install -d -m 0700 /etc/crypto-trader /var/backups/trader
install -m 0600 /root/.config/rclone/rclone.conf /etc/crypto-trader/rclone.conf
install -m 0644 deploy/systemd/crypto-trader-backup.service deploy/systemd/crypto-trader-backup.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now crypto-trader-backup.timer
```

- [ ] Run one backup and check the output:

```sh
systemctl start crypto-trader-backup.service
journalctl -u crypto-trader-backup.service -n 20 --no-pager
```

- [ ] **Expected:** two lines, `encrypted_backup=trader-<UTC>.dump.age` and
      `encrypted_manifest=trader-<UTC>.manifest.age`. If you see
      `row counts changed during pg_dump`, run it again.
- [ ] Confirm the artifacts are off-box and that no plaintext was left behind:

```sh
RCLONE_CONFIG=/etc/crypto-trader/rclone.conf rclone lsl "$(grep ^BACKUP_REMOTE= /etc/crypto-trader/backup.env | cut -d= -f2-)"
ls -la /var/backups/trader
```

  The folder listing contains only `.age` files.
- [ ] Confirm nothing secret was logged. The journal contains filenames only:

```sh
journalctl -u crypto-trader-backup.service --no-pager | grep -iE "postgres(ql)?://|password|AGE-SECRET" || echo CLEAN
```

- [ ] Confirm the timer is scheduled:
      `systemctl list-timers crypto-trader-backup.timer`

## 5. Scratch restore (operator machine, not the VPS)

- [ ] Start a throwaway scratch database. This example uses Docker; any empty
      PostgreSQL 16+ database works.

```sh
docker run -d --name trader-restore -e POSTGRES_USER=restore_user -e POSTGRES_PASSWORD=<scratch-password> -e POSTGRES_DB=trader_restore -p 127.0.0.1:5433:5432 postgres:16-alpine
```

- [ ] Pull both artifacts from the remote:

```sh
rclone copyto remote:trader/trader-<UTC>.dump.age ./trader-<UTC>.dump.age
rclone copyto remote:trader/trader-<UTC>.manifest.age ./trader-<UTC>.manifest.age
```

- [ ] Run the restore check from a checkout of the reviewed commit:

```sh
export AGE_IDENTITY_FILE=/secure/operator/trader-backup-identity.txt
export SCRATCH_DATABASE_URL=postgresql://restore_user:<scratch-password>@127.0.0.1:5433/trader_restore
sh deploy/restore-verify-postgres.sh ./trader-<UTC>.dump.age ./trader-<UTC>.manifest.age
```

- [ ] **Pass criteria:**
  - nine `verified …` lines, then `restore_verified=1`;
  - the `verified` counts match `/root/drill/after-reboot.txt` from the VPS
    (unless something was written after the reboot).
- [ ] Tear down the scratch database and delete the local copies:

```sh
docker rm -f trader-restore
rm -f ./trader-*.dump.age ./trader-*.manifest.age
```

## 6. Clean up and record

- [ ] On the VPS: `rm -rf /root/drill`. It holds the token header.
- [ ] Post the evidence as a PR or issue comment. Leave out hostnames, IPs,
      tokens, and bucket names. Do not commit the evidence, logs, or artifacts.

```text
OWNER-RUN VERIFICATION
Commit: <reviewed-commit-sha>
UTC: <date/time>
Compose startup: db+app healthy; migration 0003 -> 0004 applied
Startup recovery: <status> (kill switch <state>)
App restart: kill switch persisted (paused); row counts unchanged; recovery <status>
Host reboot: containers auto-restarted; volumes present; kill switch persisted; row counts unchanged; recovery <status>
Backup: <dump.age name>, <manifest.age name>; off-box copy confirmed; no plaintext left; journal clean
Scratch restore: restore_verified=1
  <paste the nine "verified ..." lines>
Secrets: none in artifacts or logs
Operator: <initials>
No provider writes or live-trading activation performed.
```

If every box is ticked, the owner-run acceptance for Issue #10 is complete. If
any step fails, leave the kill switch halted or paused, capture redacted output,
and follow the incident process in the runbook.
