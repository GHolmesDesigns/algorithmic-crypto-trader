#!/bin/sh

# Restores an encrypted custom-format dump into a pre-created scratch database,
# then verifies the migration version and the row count of every table named in
# the encrypted manifest written at backup time. Checking the manifest's own
# tables keeps older backups verifiable after the backup table list grows; the
# core state tables must always be present. The script never prints connection
# strings, decrypted rows, or the identity contents.
set -eu
umask 077

usage='usage: restore-verify-postgres.sh ENCRYPTED_BACKUP ENCRYPTED_MANIFEST'
backup_path=${1:?$usage}
manifest_path=${2:?$usage}
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE must be configured}"
: "${SCRATCH_DATABASE_URL:?SCRATCH_DATABASE_URL must be configured}"

# Every manifest must cover these; backup-postgres.sh lists a superset.
required_tables='signals orders fills portfolio_snapshots positions_snapshot balances_snapshot equity_curve discrepancies'

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
# Names become SQL identifiers below, so accept only simple lowercase names.
if grep -qvE '^[a-z_]+=[A-Za-z0-9_]+$' "$expected"; then
  echo "backup manifest is malformed" >&2
  exit 1
fi
grep -q '^alembic_version=' "$expected" || { echo "backup manifest lacks alembic_version" >&2; exit 1; }
for table in $required_tables; do
  grep -q "^$table=" "$expected" || { echo "backup manifest lacks required table $table" >&2; exit 1; }
done
tables=$(sed -n 's/^\([a-z_]*\)=.*/\1/p' "$expected" | grep -vx alembic_version)

# The restore and the row counts must use a PostgreSQL client of the same major version as the
# scratch database. A newer pg_restore sends settings an older server rejects (PostgreSQL 18's
# `SET transaction_timeout` against 16), and --exit-on-error rightly stops on that. When the local
# client differs or is missing, both run from the postgres image of the server's major version. The
# container reaches the database over the host network (Linux) and gets the URL through its
# environment, not its command line.
server_version_num=$(psql --dbname="$SCRATCH_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
  --command='SHOW server_version_num')
case $server_version_num in
  '' | *[!0-9]*)
    echo "could not read the scratch database's PostgreSQL version" >&2
    exit 1
    ;;
esac
server_major=$((server_version_num / 10000))
client_major=$(pg_restore --version 2>/dev/null | sed -n 's/^pg_restore (PostgreSQL) \([0-9][0-9]*\).*/\1/p')
matching_image=
if [ "$client_major" != "$server_major" ]; then
  if ! docker version >/dev/null 2>&1; then
    echo "pg_restore is PostgreSQL ${client_major:-missing} but the scratch database is PostgreSQL" \
      "$server_major: install the PostgreSQL $server_major client tools, or Docker so the matching" \
      "client can run from its image" >&2
    exit 1
  fi
  matching_image="postgres:${server_major}-alpine"
fi

restore_dump() {
  if [ -z "$matching_image" ]; then
    pg_restore --clean --if-exists --exit-on-error --no-owner --dbname="$SCRATCH_DATABASE_URL" "$plain_path"
  else
    docker run --rm -i --network host -e SCRATCH_DATABASE_URL "$matching_image" \
      sh -c 'pg_restore --clean --if-exists --exit-on-error --no-owner --dbname="$SCRATCH_DATABASE_URL"' \
      < "$plain_path"
  fi
}

count_rows() {
  if [ -z "$matching_image" ]; then
    psql --dbname="$SCRATCH_DATABASE_URL" --no-psqlrc --tuples-only --no-align \
      --set=ON_ERROR_STOP=1 --command="$query;"
  else
    docker run --rm --network host -e SCRATCH_DATABASE_URL -e "RESTORE_QUERY=$query;" "$matching_image" \
      sh -c 'psql --dbname="$SCRATCH_DATABASE_URL" --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="$RESTORE_QUERY"'
  fi
}

restore_dump

query="SELECT 'alembic_version=' || version_num FROM alembic_version"
for table in $tables; do
  query="$query UNION ALL SELECT '$table=' || count(*) FROM $table"
done
count_rows | LC_ALL=C sort > "$actual"
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
