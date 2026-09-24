# Phase 1.8 VPS drill

This drill produces the Issue #10 acceptance evidence:
- Compose startup;
- state recovery after an app restart and after a host reboot;
- encrypted backup creation;
- scratch-database restore verification.

An agent runs it under the "Agent-run infrastructure verification" rules in
[`AGENTS.md`](../AGENTS.md). The owner's part is a one-time setup in web pages
plus a yes before the reboot. See the
[deployment, backups, and restore runbook](phase-1.8-deployment-backups-restore.md)
for how each piece works.

## One-time setup

The paper deployment runs on an AWS Lightsail Ubuntu 24.04 instance with an
attached static IP. Its firewall allows only SSH, from the owner's address and
the Lightsail browser console. Backups go to a private, versioned S3 bucket in
the same account.

Owner steps, done in web pages:

1. **Agent SSH key.** Create the agent key on the owner's computer. The agent
   uploads its public half to Lightsail when it creates the instance.
2. **Backup storage login.** In IAM, create a user with only the policy below
   and an access key for "Application running outside AWS". Paste the key into
   the `rclone.conf` template the agent prepares. Never use an administrator's
   key. The agent copies the file to the VPS without opening it.
3. **GitHub secrets.** Under Settings → Secrets and variables → Actions, add:
   - `BACKUP_AGE_IDENTITY`: the backup unlock key file the agent generated.
     Keep a second copy in a password manager, because GitHub secrets cannot
     be read back and backups are unreadable without the key.
   - `BACKUP_RCLONE_CONF`: the same filled-in `rclone.conf` contents.

Backup storage policy. It can list the bucket and read or add objects under
`trader/`, and it cannot delete:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>"},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::<bucket>/trader/*"}
  ]
}
```

`/etc/crypto-trader/backup.env` sets `RCLONE_S3_NO_HEAD=true`. On a versioned
bucket, rclone otherwise re-reads each upload by version ID, which needs
`s3:GetObjectVersion`, and logs a 403 before succeeding on retry. The Restore
drill workflow verifies each backup end to end instead.

The agent prepares a fresh instance with `sudo sh deploy/bootstrap-vps.sh`.
The script installs Docker, age, and rclone, adds swap, clones the repository,
and generates `.env` with random credentials that are never printed.

## What the agent runs

On the VPS, in `/opt/algorithmic-crypto-trader`:

```sh
sh deploy/drill.sh deploy <reviewed-commit-sha>
sh deploy/drill.sh before-reboot
systemctl reboot            # only after the owner says yes in chat
sh deploy/drill.sh after-reboot
sh deploy/drill.sh backup
sh deploy/drill.sh status    # kill-switch state and startup recovery status
```

Each phase prints `CHECK <name>: PASS|FAIL …` lines and a final `RESULT`, and
exits non-zero on any failure. The drill:

- refuses to run in `live` mode or with trade-capable credentials;
- reads operator tokens only inside the app container;
- pauses trading as a marker and checks that the pause survives the app restart
  and the reboot;
- compares the migration version and every state table's row count across the
  app restart and the reboot;
- restores the original kill-switch state only if every check passed.

On GitHub, the agent then runs the **Restore drill** workflow, either from the
Actions tab or by adding the `restore-drill` label to the pull request. The
workflow downloads the newest backup, restores it into a throwaway database,
and prints `restore_verified=1` when the migration version and row counts match
the backup's manifest. It also runs every Monday and fails if the newest backup
is more than 36 hours old.

## Restart rehearsal

The **Restart rehearsal** workflow runs the same drill on a disposable GitHub
runner with sample orders, fills, and portfolio snapshots. It covers what an
empty production database cannot show:

- sample state survives an app restart and a Docker daemon restart, the stand-in
  for a reboot;
- an encrypted backup of that state restores with matching row counts;
- an unresolved pending order halts trading on the next start.

It uses throwaway credentials and keys and needs no secrets. Run it from the
Actions tab or by adding the `restart-rehearsal` label to a pull request.

## Pass criteria

- `RESULT deploy: PASS`, `RESULT before-reboot: PASS`,
  `RESULT after-reboot: PASS`, and `RESULT backup: PASS`.
- The Restore drill job succeeds with `restore_verified=1`, and the backup it
  restored is the one named by `CHECK backup-artifacts` (or newer).
- The Restart rehearsal job succeeds.

If any check fails, trading stays paused or halted. The agent reports the
failed checks and the owner decides the next step.

## Evidence comment

```text
OWNER-RUN VERIFICATION (agent-run with owner approval)
Commit: <reviewed-commit-sha>
UTC: <date/time>
Deploy: <RESULT line>; migration 0004; startup recovery <status>
App restart: <RESULT line>
Host reboot: <RESULT line>; kill switch left <state>
Backup: <RESULT line>; <dump.age name>, <manifest.age name>
Restore drill: <workflow run URL>; restore_verified=1; <N> checks
Secrets: none printed; no hostnames, IPs, bucket names, or tokens recorded
No provider writes or live-trading activation performed.
```
