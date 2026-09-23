#!/bin/sh

# Restores an encrypted custom-format dump into a pre-created scratch database,
# then verifies the migration version and the row count of every durable state
# table against the encrypted manifest written at backup time. The script never
# prints connection strings, decrypted rows, or the identity contents.
set -eu
umask 077

usage='usage: restore-verify-postgres.sh ENCRYPTED_BACKUP ENCRYPTED_MANIFEST'
backup_path=${1:?$usage}
manifest_path=${2:?$usage}
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE must be configured}"
: "${SCRATCH_DATABASE_URL:?SCRATCH_DATABASE_URL must be configured}"

# Keep this list identical to backup-postgres.sh.
tables='signals orders fills portfolio_snapshots positions_snapshot balances_snapshot equity_curve discrepancies'

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/trader-restore.XXXXXX")
cleanup() {
  rm -rf -- "$work_dir"
}
trap cleanup EXIT HUP INT TERM
plain_path="$work_dir/backup.dump"
expected="$work_dir/expected"
actual="$work_dir/actual"

test -s "$backup_path"
test -s "$manifest_path"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$plain_path" "$backup_path"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$expected" "$manifest_path"
test -s "$plain_path"
test -s "$expected"
pg_restore --clean --if-exists --exit-on-error --no-owner --dbname="$SCRATCH_DATABASE_URL" "$plain_path"

query="SELECT 'alembic_version=' || version_num FROM alembic_version"
for table in $tables; do
  query="$query UNION ALL SELECT '$table=' || count(*) FROM $table"
done
psql --dbname="$SCRATCH_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
  --set=ON_ERROR_STOP=1 --command="$query;" | LC_ALL=C sort > "$actual"
test -s "$actual"

if ! cmp -s "$expected" "$actual"; then
  echo "restored contents do not match the backup manifest" >&2
  diff "$expected" "$actual" >&2 || true
  exit 1
fi

# Table names, row counts, and the migration version are not secret; they are the
# evidence to record in the private operations log.
sed 's/^/verified /' "$actual"
printf 'restore_verified=1\n'
