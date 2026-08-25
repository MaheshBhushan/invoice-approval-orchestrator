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

## Kubernetes with kind

Requirements: Docker, kind, Helm 3, kubectl, and approximately 4 GB available memory.

Create a local cluster, build and load the application image, and install the chart:

```sh
kind create cluster --name invoice-approval
docker build -t invoice-approval-orchestrator:local -f services/Dockerfile .
kind load docker-image invoice-approval-orchestrator:local --name invoice-approval
helm install invoice-approval deploy/helm/invoice-approval-orchestrator
```

The chart creates single-replica deployments for the FastAPI service, workers, finance mock, PostgreSQL, and Camunda. It also creates Services, PostgreSQL and Camunda ConfigMaps, a PostgreSQL Secret, and persistent volume claims for PostgreSQL and Camunda.

Check the installed resources:

```sh
kubectl get deployments,services,pvc
```

Access a service by running the relevant command in a separate terminal:

```sh
kubectl port-forward service/invoice-api 8000:8000
kubectl port-forward service/finance-mock 8001:8001
kubectl port-forward service/orchestration 8088:8080
```

The API is then available at <http://localhost:8000/docs>, the finance mock at <http://localhost:8001/docs>, and Camunda at <http://localhost:8088>.

Remove the release and cluster when finished:

```sh
helm uninstall invoice-approval
kind delete cluster --name invoice-approval
```

This chart is a local portfolio deployment, not a production platform. It uses development credentials from `values.yaml`, single replicas, small default volumes, no ingress or TLS, and no high-availability or managed-secret setup. It does not deploy n8n, so the optional human-review notification webhook is disabled; Camunda still owns the human task. GitHub Actions uses the same build, image-load, and install path with the `ci` image tag. CI verifies that Kubernetes accepts the release but deliberately does not wait for workload readiness.

## Deployable path

Camunda can be moved to Camunda 8 SaaS by changing `CAMUNDA_REST_URL` and the Zeebe connection settings in `compose.yaml`. Run the Python API, workers, finance adapter, and PostgreSQL on a managed container platform, keep secrets in its secret store, and replace the finance mock with the customer's REST or UiPath adapter. n8n can be self-hosted or replaced by an existing integration platform. This repository does not claim a live public endpoint.
