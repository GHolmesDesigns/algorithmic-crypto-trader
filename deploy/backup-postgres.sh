#!/bin/sh

# Creates an encrypted custom-format dump and copies only the encrypted artifact
# to the configured off-box rclone destination. No secret-bearing value is logged.
set -eu
umask 077

: "${DATABASE_URL:?DATABASE_URL must be configured}"
: "${BACKUP_REMOTE:?BACKUP_REMOTE must be configured (for example, s3:bucket/trader)}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT must be configured}"

backup_dir=${BACKUP_DIR:-/var/backups/trader}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
name="trader-${timestamp}.dump.age"
encrypted_path="${backup_dir}/${name}"
plain_path="${backup_dir}/.${name}.plain"

cleanup() {
  rm -f -- "$plain_path"
}
trap cleanup EXIT HUP INT TERM

mkdir -p -- "$backup_dir"
pg_dump --format=custom --no-owner --no-acl --dbname="$DATABASE_URL" > "$plain_path"
test -s "$plain_path"
age --recipient "$AGE_RECIPIENT" --output "$encrypted_path" "$plain_path"
test -s "$encrypted_path"
rm -f -- "$plain_path"
rclone copyto --immutable "$encrypted_path" "${BACKUP_REMOTE%/}/${name}"

printf 'encrypted_backup=%s\n' "$name"
