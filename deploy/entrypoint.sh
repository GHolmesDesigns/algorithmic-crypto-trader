#!/bin/sh

# Migrations must complete before the process can accept operator or trading work.
# Do not enable shell tracing here: DATABASE_URL can contain credentials.
set -eu

: "${DATABASE_URL:?DATABASE_URL must be configured}"

alembic upgrade head
exec "$@"
