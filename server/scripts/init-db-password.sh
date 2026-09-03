#!/bin/bash
# Reset the Postgres password for the homeservices user.
#
# The pgdata volume remembers the password from first init. If HOME_DB_PASSWORD
# changes in .env, the DB still has the old password. This script fixes the mismatch.
#
# Usage (from the server):
#   # Option 1: run against the infra DB container
#   docker exec -i home-services-db psql -U homeservices -d home_services \
#     -c "ALTER USER homeservices WITH PASSWORD '${HOME_DB_PASSWORD}'"
#
#   # Option 2: run this script (reads from .env)
#   cd ~/lios-core && bash scripts/init-db-password.sh
#
# Idempotent — safe to run repeatedly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
fi

DB_PASSWORD="${HOME_DB_PASSWORD:?HOME_DB_PASSWORD not set — check .env}"
DB_USER="${HOME_DB_USER:-homeservices}"
DB_NAME="home_services"
DB_CONTAINER="home-services-db"

echo "Updating password for user '${DB_USER}' in container '${DB_CONTAINER}'..."

docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" \
    -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}'"

echo "Done. Password updated."
