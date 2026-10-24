#!/bin/sh
# Loads the schema, seed and reconciliation views into a throwaway PostgreSQL
# container and runs the SQL assertions in tests/sql/test_reconciliation.sql.
set -eu

cd "$(dirname "$0")/.."
name="recon-test-$$"
docker run -d --rm --name "$name" -e POSTGRES_PASSWORD=test \
  -v "$PWD/db/init:/docker-entrypoint-initdb.d:ro" \
  -v "$PWD/tests/sql:/tests:ro" \
  "postgres:${POSTGRES_VERSION:-17-alpine}" >/dev/null
trap 'docker stop "$name" >/dev/null 2>&1 || true' EXIT
i=0
until docker exec "$name" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { docker logs "$name"; exit 1; }; sleep 0.5
done
docker exec "$name" psql -q -U postgres -f /tests/test_reconciliation.sql
