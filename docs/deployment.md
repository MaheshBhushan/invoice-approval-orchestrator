# Deployment

## Local demonstration

Requirements: Docker Compose v2 and 4 GB available memory.

```sh
cp .env.example .env
docker compose up -d --build --wait
./scripts/demo.sh
```

Services: API <http://localhost:8000/docs>, finance mock <http://localhost:8001/docs>, Camunda <http://localhost:8088>, and n8n <http://localhost:5678>.

Run the integration suite with `./scripts/test.sh`. Stop with `docker compose down`; add `-v` only when intentionally resetting seeded database state.

## Deployable path

Camunda can be moved to Camunda 8 SaaS by changing `CAMUNDA_REST_URL` and the Zeebe connection settings in `compose.yaml`. Run the Python API, workers, finance adapter, and PostgreSQL on a managed container platform, keep secrets in its secret store, and replace the finance mock with the customer's REST or UiPath adapter. n8n can be self-hosted or replaced by an existing integration platform. This repository does not claim a live public endpoint.
