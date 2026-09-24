#!/bin/sh

# Automated VPS restart drill for Issue #10. An agent or operator runs the
# phases in order, with a host reboot between `before-reboot` and
# `after-reboot`:
#
#   drill.sh deploy <commit-sha>   check out, rebuild, wait for health
#   drill.sh before-reboot         pause marker, row-count baseline, app restart check
#   systemctl reboot               (only with owner approval for the window)
#   drill.sh after-reboot          post-reboot checks, restore the original kill-switch state
#   drill.sh backup                install the nightly backup and run it once
#   drill.sh status                print the kill-switch state and startup recovery status
#
# Operator tokens are read inside the app container from its own environment;
# they never appear on the host command line or in output. The drill refuses to
# run against a live or trade-capable configuration.
set -eu
umask 077

project_dir=${COMPOSE_PROJECT_DIR:-/opt/algorithmic-crypto-trader}
state_dir=${DRILL_STATE_DIR:-/var/lib/crypto-trader-drill}
failures=0

compose() {
  docker compose --project-directory "$project_dir" \
    -f "$project_dir/docker-compose.yml" \
    -f "$project_dir/deploy/docker-compose.vps.yml" \
    --env-file "$project_dir/.env" "$@"
}

# ok CMD... prints 0 when CMD succeeds and 1 otherwise, without tripping `set -e`.
ok() {
  if "$@" >/dev/null 2>&1; then echo 0; else echo 1; fi
}

check() {
  # check NAME RESULT DETAIL, where RESULT comes from ok
  if [ "$2" -eq 0 ]; then
    printf 'CHECK %s: PASS %s\n' "$1" "$3"
  else
    printf 'CHECK %s: FAIL %s\n' "$1" "$3"
    failures=$((failures + 1))
  fi
}

finish() {
  if [ "$failures" -eq 0 ]; then
    printf 'RESULT %s: PASS\n' "$1"
  else
    printf 'RESULT %s: FAIL (%s failed checks)\n' "$1" "$failures"
    exit 1
  fi
}

env_value() {
  sed -n "s/^$1=//p" "$project_dir/.env" | tail -n 1
}

require_paper() {
  mode=$(env_value TRADING_MODE)
  scope=$(env_value CREDENTIAL_SCOPE)
  if [ "${mode:-paper}" = live ] || [ "${scope:-none}" = trade ]; then
    echo "refusing: the drill runs only with a non-live, non-trade configuration" >&2
    exit 2
  fi
}

# api METHOD PATH ROLE [FIELD] prints the JSON response, or one dotted field of it.
api() {
  compose exec -T app python -c '
import json, os, sys, urllib.request
method, path, role = sys.argv[1:4]
token = os.environ["OPERATOR_ADMIN_TOKEN" if role == "admin" else "OPERATOR_TOKEN"]
request = urllib.request.Request(
    "http://127.0.0.1:8000" + path,
    method=method,
    headers={"x-operator-token": token, "accept": "application/json"},
)
body = json.loads(urllib.request.urlopen(request, timeout=10).read())
for key in sys.argv[4].split(".") if len(sys.argv) > 4 else ():
    body = body[key]
print(body if isinstance(body, str) else json.dumps(body, sort_keys=True))
' "$@"
}

counts() {
  compose exec -T db sh -c \
    'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1' <<'SQL' | LC_ALL=C sort
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
}

wait_healthy() {
  attempt=0
  while [ "$attempt" -lt 60 ]; do
    db=$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q db)" 2>/dev/null || true)
    app=$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q app)" 2>/dev/null || true)
    if [ "$db" = healthy ] && [ "$app" = healthy ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  return 1
}

recovery_logged() {
  # Match the structured log entry, not a logging error that merely quotes the text.
  compose logs --no-color app 2>/dev/null | grep -q '"message": "startup recovery '
}

record_recovery() {
  status=$(api GET /operator/state operator recovery.status || echo unavailable)
  printf '%s\n' "$status" > "$state_dir/$1-recovery"
  check "$1-recovery" "$(ok recovery_ok "$status")" "status=$status"
}

recovery_ok() {
  [ "$1" = reconciled ] || [ "$1" = no_broker ]
}

cd "$project_dir"
require_paper
mkdir -p -m 0700 "$state_dir"

case "${1:-}" in
  deploy)
    sha=${2:?usage: drill.sh deploy <commit-sha>}
    git fetch --quiet origin
    git checkout --quiet --detach "$sha"
    compose up -d --build --quiet-pull >/dev/null
    check deploy-health "$(ok wait_healthy)" "db and app healthy"
    check deploy-commit "$(ok test "$(git rev-parse HEAD)" = "$(git rev-parse "$sha")")" "$(git rev-parse --short HEAD)"
    migration=$(counts | sed -n 's/^alembic_version=//p')
    check deploy-migration "$(ok test "$migration" = 0004_portfolio_snapshot_batches)" "alembic_version=$migration"
    check deploy-recovery-logged "$(ok recovery_logged)" "startup recovery line present"
    record_recovery deploy
    finish deploy
    ;;

  before-reboot)
    api GET /operator/kill-switch operator state > "$state_dir/original-kill-switch"
    api POST /operator/pause operator state >/dev/null
    counts > "$state_dir/before.txt"
    check baseline-counts "$(ok test -s "$state_dir/before.txt")" "$(wc -l < "$state_dir/before.txt") rows recorded"
    compose restart app >/dev/null
    check app-restart-health "$(ok wait_healthy)" "db and app healthy"
    state=$(api GET /operator/kill-switch operator state || echo unavailable)
    check app-restart-kill-switch "$(ok test "$state" = paused)" "state=$state"
    counts > "$state_dir/after-app-restart.txt"
    check app-restart-rows "$(ok cmp -s "$state_dir/before.txt" "$state_dir/after-app-restart.txt")" "row counts unchanged"
    record_recovery app-restart
    finish before-reboot
    ;;

  after-reboot)
    test -s "$state_dir/before.txt" || { echo "run before-reboot first" >&2; exit 2; }
    check reboot-health "$(ok wait_healthy)" "containers restarted on their own and are healthy"
    volumes=$(docker volume ls --format '{{.Name}}' | grep -cE '_(postgres-data|kill-switch-data)$' || true)
    check reboot-volumes "$(ok test "$volumes" -eq 2)" "$volumes of 2 named volumes present"
    state=$(api GET /operator/kill-switch operator state || echo unavailable)
    check reboot-kill-switch "$(ok test "$state" = paused)" "state=$state"
    counts > "$state_dir/after-reboot.txt"
    check reboot-rows "$(ok cmp -s "$state_dir/before.txt" "$state_dir/after-reboot.txt")" "row counts unchanged"
    record_recovery reboot
    original=$(cat "$state_dir/original-kill-switch")
    if [ "$failures" -eq 0 ] && [ "$original" = running ]; then
      restored=$(api POST /operator/rearm admin state || echo unavailable)
    else
      restored=$(api GET /operator/kill-switch operator state || echo unavailable)
    fi
    printf 'INFO kill switch left %s (was %s before the drill)\n' "$restored" "$original"
    printf 'INFO row counts:\n'
    sed 's/^/INFO   /' "$state_dir/after-reboot.txt"
    finish after-reboot
    ;;

  backup)
    for path in /etc/crypto-trader/backup.env /etc/crypto-trader/rclone.conf; do
      test -s "$path" || { echo "missing $path" >&2; exit 2; }
      chmod 0600 "$path"
    done
    install -d -m 0700 /var/backups/trader
    install -m 0644 deploy/systemd/crypto-trader-backup.service /etc/systemd/system/
    install -m 0644 deploy/systemd/crypto-trader-backup.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now crypto-trader-backup.timer >/dev/null 2>&1
    started=$(date -u '+%Y-%m-%d %H:%M:%S')
    check backup-run "$(ok systemctl start crypto-trader-backup.service)" "backup service completed"
    output=$(journalctl -u crypto-trader-backup.service --since "$started UTC" --no-pager -o cat)
    dump=$(printf '%s\n' "$output" | sed -n 's/^encrypted_backup=//p' | tail -n 1)
    manifest=$(printf '%s\n' "$output" | sed -n 's/^encrypted_manifest=//p' | tail -n 1)
    check backup-artifacts "$(ok test -n "$dump" -a -n "$manifest")" "$dump $manifest"
    # A retried upload still completes; any error line means the setup needs attention.
    errors=$(printf '%s
' "$output" | grep -cE 'ERROR|Forbidden|AccessDenied' || true)
    check backup-log-clean "$(ok test "$errors" -eq 0)" "$errors error lines in this run's log"
    remote=$(sed -n 's/^BACKUP_REMOTE=//p' /etc/crypto-trader/backup.env | tail -n 1)
    listed=$(RCLONE_CONFIG=/etc/crypto-trader/rclone.conf rclone lsf "$remote" 2>/dev/null \
      | grep -cxF -e "$dump" -e "$manifest" || true)
    check backup-offbox "$(ok test "$listed" -eq 2)" "$listed of 2 artifacts present off-box"
    plaintext=$(find /var/backups/trader -type f ! -name '*.age' | wc -l)
    check backup-no-plaintext "$(ok test "$plaintext" -eq 0)" "$plaintext non-encrypted files left on disk"
    journalctl -u crypto-trader-backup.service --no-pager -o cat \
      | grep -qiE 'postgres(ql)?://|password|AGE-SECRET-KEY' && leaked=1 || leaked=0
    check backup-journal-clean "$(ok test "$leaked" -eq 0)" "no connection strings, passwords, or keys logged"
    check backup-timer "$(ok systemctl is-enabled --quiet crypto-trader-backup.timer)" "nightly timer enabled"
    finish backup
    ;;

  status)
    printf 'kill_switch=%s
' "$(api GET /operator/kill-switch operator state || echo unavailable)"
    printf 'recovery=%s
' "$(api GET /operator/state operator recovery.status || echo unavailable)"
    ;;

  *)
    echo "usage: drill.sh deploy <sha> | before-reboot | after-reboot | backup | status" >&2
    exit 2
    ;;
esac
