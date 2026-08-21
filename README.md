# Invoice Workflow Agent

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

Run all integration tests with `./scripts/test.sh`. The intake endpoint accepts `POST /invoices` and an `Idempotency-Key`; replaying the same key returns the original process instance.

## Reliability and explainability

- Camunda service jobs use durable retries for transient worker and finance failures.
- Invoice and finance-posting uniqueness constraints make retries and duplicate delivery safe.
- Low agent confidence (below `0.80`) routes to a human rather than silently posting.
- Agent output includes classification, confidence, evidence, and a plain-language explanation; deterministic policy owns the final route.
- PostgreSQL stores invoice state, append-only audit events, process metrics, and finance postings.
- Human approval records reviewer identity and rejection reason.

## Benchmark

The checked-in [benchmark/results.md](benchmark/results.md) records a 100-invoice run: 100/100 completed, 5.61 s mean completion latency, 90% straight-through processing, 10% human review, 10 duplicate deliveries prevented, and 100% classification accuracy. The run used deterministic seeded data and recorded zero transient retries, so it is a demonstration benchmark rather than a production capacity claim. Re-run it with `python scripts/benchmark.py` against the running stack.

## Repository map

`bpmn/` executable Camunda process · `services/` FastAPI, workers, agent, and finance adapter · `db/` PostgreSQL schema and seeds · `n8n/` importable workflows · `tests/` unit and integration tests · `scripts/` demo, recovery, and benchmark commands · `docs/` architecture and deployment notes.

## Limitations

This is a reference implementation: invoice extraction uses supplied structured demo payloads, the finance service is a mock, and authentication, multi-tenancy, production OCR, model monitoring, and accounting-specific compliance controls are not included. A production deployment must add those controls, use managed secrets, replace the finance adapter, and validate retention and approval policy with the accounting owner. UiPath is an adapter extension point, not a required local dependency.

## CI and deployment

GitHub Actions runs Python syntax checks, unit tests, BPMN XML validation, Compose validation, and the application image build. See [docs/deployment.md](docs/deployment.md) for the local setup and a Camunda SaaS/container deployment path. No live public endpoint is claimed by this repository.

## License

MIT; see [LICENSE](LICENSE).
