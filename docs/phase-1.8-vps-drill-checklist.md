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

## One-time owner setup

1. **Agent SSH access.** Install the agent's public SSH key for the VPS login
   user, usually through the provider's web console, and tell the agent the
   server address. The key can be removed at any time.
2. **Backup storage.** Create a private bucket, for example on Backblaze B2, and
   an application key limited to that bucket. Fill the key ID, key, and bucket
   name into the `rclone.conf` template the agent prepares on the owner's
   computer. The agent copies that file to the VPS without opening it.
3. **GitHub secrets.** Under the repository's Settings → Secrets and variables →
   Actions, add:
   - `BACKUP_AGE_IDENTITY`: the backup unlock key file the agent generated.
     Keep a second copy in a password manager, because GitHub secrets cannot
     be read back and backups are unreadable without the key.
   - `BACKUP_RCLONE_CONF`: the same filled-in `rclone.conf` contents.

## What the agent runs

On the VPS, in `/opt/algorithmic-crypto-trader`:

```sh
sh deploy/drill.sh deploy <reviewed-commit-sha>
sh deploy/drill.sh before-reboot
systemctl reboot            # only after the owner says yes in chat
sh deploy/drill.sh after-reboot
sh deploy/drill.sh backup
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

## Pass criteria

- `RESULT deploy: PASS`, `RESULT before-reboot: PASS`,
  `RESULT after-reboot: PASS`, and `RESULT backup: PASS`.
- The Restore drill job succeeds with `restore_verified=1`, and the backup it
  restored is the one named by `CHECK backup-artifacts` (or newer).

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
