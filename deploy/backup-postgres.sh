#!/bin/sh

# Creates an encrypted custom-format dump plus an encrypted row-count manifest and
# copies only the encrypted artifacts to the configured off-box rclone destination.
# pg_dump and psql run inside the Compose `db` container with the container's own
# credentials, so the host never needs a database URL, network access to the
# database, or matching PostgreSQL client tools. No secret-bearing value is logged.
set -eu
umask 077

: "${BACKUP_REMOTE:?BACKUP_REMOTE must be configured (for example, s3:bucket/trader)}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT must be configured}"

project_dir=${COMPOSE_PROJECT_DIR:-/opt/algorithmic-crypto-trader}
backup_dir=${BACKUP_DIR:-/var/backups/trader}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
name="trader-${timestamp}.dump.age"
manifest_name="trader-${timestamp}.manifest.age"
encrypted_path="${backup_dir}/${name}"
encrypted_manifest="${backup_dir}/${manifest_name}"
plain_path="${backup_dir}/.${name}.plain"
manifest_before="${backup_dir}/.${manifest_name}.before"
manifest_after="${backup_dir}/.${manifest_name}.after"

# Keep this list identical to restore-verify-postgres.sh.
tables='signals orders fills system_events audit_notes market_candles portfolio_snapshots positions_snapshot balances_snapshot equity_curve discrepancies'

cleanup() {
  rm -f -- "$plain_path" "$manifest_before" "$manifest_after"
}
trap cleanup EXIT HUP INT TERM

compose() {
  docker compose --project-directory "$project_dir" \
    -f "$project_dir/docker-compose.yml" \
    -f "$project_dir/deploy/docker-compose.vps.yml" \
    --env-file "$project_dir/.env" "$@"
}

manifest_query() {
  query="SELECT 'alembic_version=' || version_num FROM alembic_version"
  for table in $tables; do
    query="$query UNION ALL SELECT '$table=' || count(*) FROM $table"
  done
  printf '%s;' "$query"
}

write_manifest() {
  compose exec -T db sh -c \
    'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="$1"' \
    manifest "$(manifest_query)" | LC_ALL=C sort > "$1"
  test -s "$1"
}

mkdir -p -- "$backup_dir"

# Counts are taken before and after the dump; equal counts mean the manifest
# describes the dumped data. A write during the dump fails the run for retry.
write_manifest "$manifest_before"
compose exec -T db sh -c \
  'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --no-owner --no-acl' \
  > "$plain_path"
write_manifest "$manifest_after"
test -s "$plain_path"
if ! cmp -s "$manifest_before" "$manifest_after"; then
  echo "row counts changed during pg_dump; backup discarded, retry" >&2
  exit 1
fi

age --recipient "$AGE_RECIPIENT" --output "$encrypted_path" "$plain_path"
age --recipient "$AGE_RECIPIENT" --output "$encrypted_manifest" "$manifest_after"
test -s "$encrypted_path"
test -s "$encrypted_manifest"
rm -f -- "$plain_path" "$manifest_before" "$manifest_after"

rclone copyto --immutable "$encrypted_path" "${BACKUP_REMOTE%/}/${name}"
rclone copyto --immutable "$encrypted_manifest" "${BACKUP_REMOTE%/}/${manifest_name}"

printf 'encrypted_backup=%s\n' "$name"
printf 'encrypted_manifest=%s\n' "$manifest_name"
