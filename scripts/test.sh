#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
docker compose up -d --build --wait
docker compose run --rm --no-deps \
  -e API_URL=http://invoice-api:8000 \
  -e CAMUNDA_URL=http://orchestration:8080 \
  -e FINANCE_URL=http://finance-mock:8001 \
  -e DATABASE_URL=postgresql://invoice_app:invoice_dev@postgres:5432/invoice_workflow \
  -v "$PWD/tests:/app/tests:ro" \
  invoice-api pytest -q /app/tests
./scripts/demo.sh
