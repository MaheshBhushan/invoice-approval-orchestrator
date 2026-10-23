# Invoice Approval Orchestrator

An executable invoice-processing reference system combining **BPMN 2.0**, **Workflow Automation**, Camunda 8, Python, SQL, and an explainable LangGraph **KI-Agenten** workflow. It extracts invoice data, validates suppliers and purchase orders, classifies discrepancies, sends uncertain cases to a human, updates a finance system exactly once, and records an audit trail.

## Why this is a real process

Accounts-payable teams spend time comparing invoices with supplier and purchase-order data, chasing exceptions, and proving who approved a payment. This project automates the repeatable path while preserving human control for ambiguous cases. The measurable demo result is 90% straight-through processing, 10% human review, 100% seeded classification accuracy, and 10 duplicate deliveries prevented.

## Architecture

The editable architecture diagram is [docs/architecture.md](docs/architecture.md). The executable process is [bpmn/invoice-approval.bpmn](bpmn/invoice-approval.bpmn), with a Camunda human-approval form in [forms/](forms/).

## Run the local demo

Requirements: Docker Compose v2 and 4 GB available memory.

```sh
cp .env.example .env
docker compose up -d --build --wait
./scripts/demo.sh
```

Open <http://localhost:8000/docs> for the REST API, <http://localhost:8088> for Camunda, and <http://localhost:5678> for n8n. Seeded scenarios include a valid invoice, unknown supplier, PO amount mismatch, and an ambiguous surcharge. The ambiguous case appears as a Camunda human task; approve or reject it in Tasklist and inspect the resulting audit event in PostgreSQL.

The shared version 1 OpenAPI contract is served at <http://localhost:8000/api/v1/openapi.json>. It covers `POST /invoices` and Camunda's user-task completion operation. Consumer request examples live under `contracts/consumers/`; run `python3 tests/test_contracts.py` to validate both fixtures and check changes against the frozen v1 compatibility baseline. Breaking changes require a new contract version.

Run all integration tests with `./scripts/test.sh`. The intake endpoint accepts `POST /invoices` and an `Idempotency-Key`; replaying the same key returns the original process instance.

## Reliability and explainability

- Camunda service jobs use durable retries for transient worker and finance failures.
- Invoice and finance-posting uniqueness constraints make retries and duplicate delivery safe.
- Low agent confidence (below `0.80`) routes to a human rather than silently posting.
- Agent output includes classification, confidence, evidence, and a plain-language explanation; deterministic policy owns the final route.
- PostgreSQL stores invoice state, append-only audit events, process metrics, and finance postings.
- Human approval records reviewer identity and rejection reason.

## Data quality statistics

`db/init/003_data_quality.sql` creates the `data_quality` schema with descriptive SQL views:

- `missing_field_summary`: missing or empty intake fields and unresolved supplier or purchase order references.
- `discrepancy_summary`: invoice counts per validation failure or agent discrepancy category.
- `processing_delay_summary`: delay buckets with average, p50, p95 and maximum delay plus retry totals.

Each summary has a matching `*_findings` view with one row per source invoice, and every summary row carries `invoice_ids` (and `source_event_id` for validation categories) so a statistic traces back to its input records. Run `./scripts/test_data_quality.sh` to check the views against a throwaway PostgreSQL container.

## Benchmark

The checked-in [benchmark/results.md](benchmark/results.md) records a 100-invoice run: 100/100 completed, 5.61 s mean completion latency, 90% straight-through processing, 10% human review, 10 duplicate deliveries prevented, and 100% classification accuracy. The run used deterministic seeded data and recorded zero transient retries, so it is a demonstration benchmark rather than a production capacity claim. Re-run it with `python scripts/benchmark.py` against the running stack.

## Repository map

`bpmn/` executable Camunda process · `services/` FastAPI, workers, agent, and finance adapter · `db/` PostgreSQL schema and seeds · `n8n/` importable workflows · `deploy/helm/` Kubernetes deployment · `tests/` unit and integration tests · `scripts/` demo, recovery, and benchmark commands · `docs/` architecture and deployment notes.

## Limitations

This is a reference implementation: invoice extraction uses supplied structured demo payloads, the finance service is a mock, and authentication, multi-tenancy, production OCR, model monitoring, and accounting-specific compliance controls are not included. A production deployment must add those controls, use managed secrets, replace the finance adapter, and validate retention and approval policy with the accounting owner. UiPath is an adapter extension point, not a required local dependency.

## CI and deployment

The Helm chart deploys the FastAPI service, workers, finance mock, PostgreSQL, and Camunda with configuration, Secrets, Services, and persistent volumes. GitHub Actions runs Python syntax checks, unit tests, BPMN XML validation, Compose validation, and image builds, then creates a kind cluster, loads the application image, and verifies `helm install` on every push and pull request. See [docs/deployment.md](docs/deployment.md) for Compose and kind instructions, access commands, and deployment limits. No live public endpoint is claimed by this repository.

## License

MIT; see [LICENSE](LICENSE).
