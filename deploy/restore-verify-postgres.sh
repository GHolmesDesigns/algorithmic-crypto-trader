#!/bin/sh

# Restores an encrypted custom-format dump into a pre-created scratch database,
# then verifies the migration marker and every durable state table. The script
# never prints connection strings, decrypted data, or the identity contents.
set -eu
umask 077

backup_path=${1:?usage: restore-verify-postgres.sh ENCRYPTED_BACKUP}
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE must be configured}"
: "${SCRATCH_DATABASE_URL:?SCRATCH_DATABASE_URL must be configured}"

plain_path=$(mktemp "${TMPDIR:-/tmp}/trader-restore.XXXXXX.dump")
cleanup() {
  rm -f -- "$plain_path"
}
trap cleanup EXIT HUP INT TERM

test -s "$backup_path"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$plain_path" "$backup_path"
test -s "$plain_path"
pg_restore --clean --if-exists --exit-on-error --no-owner --dbname="$SCRATCH_DATABASE_URL" "$plain_path"

tables='alembic_version signals orders fills positions_snapshot balances_snapshot equity_curve discrepancies'
for table in $tables; do
  present=$(psql --dbname="$SCRATCH_DATABASE_URL" --tuples-only --no-align \
    --command="SELECT to_regclass('$table') IS NOT NULL;")
  test "$(printf '%s' "$present" | tr -d '[:space:]')" = "t"
done

printf 'restore_verified=1\n'
