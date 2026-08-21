# Architecture

```mermaid
flowchart TD
  N[n8n intake / webhook] --> API[FastAPI invoice API]
  API --> C[Camunda 8 BPMN 2.0 process]
  C --> E[Python extraction worker]
  E --> V[Supplier + PO validation]
  V --> A[LangGraph KI-Agent]
  A --> G{confidence < 0.80?}
  G -- no --> F[Idempotent finance REST API]
  G -- yes --> H[Camunda human approval task]
  H -- approve --> F
  H -- reject --> R[Business rejection]
  API --> DB[(PostgreSQL: invoices, audit, metrics)]
  E --> DB
  V --> DB
  A --> DB
  F --> DB
  H -. notification .-> N
```

The BPMN source is [bpmn/invoice-approval.bpmn](../bpmn/invoice-approval.bpmn); it is the executable source of truth. PostgreSQL constraints and invoice IDs provide idempotency. Camunda job retries handle transient worker failures, while non-retryable validation failures become explicit business rejections. The agent supplies a structured classification, confidence, evidence, and explanation; deterministic policy decides whether a human is required.
