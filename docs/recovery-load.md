# Recovery and concurrent load harness

The harness runs against the existing Python, PostgreSQL, and Camunda services in
the separate `invoice-recovery` Compose project. It creates uniquely named demo
invoices and retains their records for inspection. It uses separate volumes and
random ports bound to `127.0.0.1`. It leaves the normal demo project untouched.

Requirements: Docker, Compose 2.24.4 or later, Python 3, curl, and enough memory
for another Camunda instance. Keep `.env` on the checked-in demo database values
for this local harness. Run commands from the repository root.

## Start and deploy

```sh
docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml up -d --build postgres orchestration finance-mock finance-fault-proxy invoice-api invoice-workers
docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml ps
```

After PostgreSQL, Camunda, the API, and the proxy report healthy, deploy the
original BPMN and form into this project's Camunda instance:

```sh
CAMUNDA_ADDRESS=$(docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml port orchestration 8080)
curl -fsS --max-time 10 -X POST "http://$CAMUNDA_ADDRESS/v2/deployments" \
  -F resources=@bpmn/invoice-approval.bpmn \
  -F resources=@forms/invoice-human-approval.form
```

Run each scenario separately. Each default run submits four invoices, uses four
concurrent clients, and sends three duplicate intake and finance deliveries per
invoice. These repeated REST deliveries represent duplicate callbacks in the
current synchronous finance contract.

```sh
python scripts/recovery_load.py --scenario restart
python scripts/recovery_load.py --scenario delay
python scripts/recovery_load.py --scenario outage
```

`--count`, `--concurrency`, and `--timeout` control the load and completion deadline.
`--output benchmark/results-recovery-delay.json` saves observed evidence. The
final JSON line includes invoice IDs, fault events, posting and retry counts, and
elapsed time. A failed invariant or deadline produces a nonzero exit. Run one
scenario at a time because the proxy has one fault configuration.

## Fault schedule and checks

| Scenario | Deterministic trigger | Recovery evidence |
|---|---|---|
| `restart` | Proxy commits the first finance posting and holds the response; harness observes the barrier and sends SIGKILL to the worker | The restarted worker reclaims the expired job; the interrupted finance attempt remains `STARTED`; exactly one posting and completion survive |
| `delay` | Proxy delays the first response for each invoice by one second after finance commits | Worker's 0.5 second HTTP deadline expires; durable retry reuses the committed posting |
| `outage` | Proxy closes the first TCP connection for each invoice before contacting finance | Each invoice records a retryable failure and completes through a subsequent request |

The proxy claims each fault once per invoice under a lock. Later concurrent
callbacks and worker retries pass through. The restart case restores workers and
releases held responses in a `finally` block. Other services remain running.
The override shortens the finance job lease to two seconds for recovery checks;
the ordinary stack keeps its ten-second HTTP timeout and job lease.

The verifier requires `POSTED` state, completed process metrics, one finance
posting, consistent external references, contiguous attempt numbers, successful
steps, and matching retry counters, attempts, and failure audit events. It requires
one receipt, process start, finance-posted event, and completion event. It checks
lifecycle ordering and compares the audit checkpoint with the final audit history.
An unfinished finance attempt is permitted only in the worker restart case.

## Regression checks

```sh
docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml run --rm --no-deps -T \
  -e RECOVERY_DB_TEST=1 -v ./tests:/app/tests:ro -v ./scripts:/app/scripts:ro \
  invoice-api pytest -q /app/tests/test_recovery.py /app/tests/test_recovery_db.py /app/tests/test_classifier.py
```

The database checks issue concurrent first finance deliveries and concurrent
attempt allocations. They require one finance reference and contiguous attempt
numbers. A reused key for another invoice must return HTTP 409. The fast checks
exercise fault selection and reject deliberately corrupted verifier evidence.

## Diagnose a failed run

1. Save the failing command, exception, scenario, and printed invoice IDs.
2. Inspect worker and proxy logs:
   `docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml logs --tail 100 invoice-workers finance-fault-proxy`.
3. Inspect the affected records with
   `docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml exec postgres psql -U invoice_app -d invoice_workflow`.
   Query `invoices`, `process_metrics`, `processing_attempts`, `finance_postings`,
   and `audit_events` by the printed `invoice_id`. Order audit events by `event_id`
   and attempts by `step, attempt_number`.
4. Check Camunda incidents for the recorded `process_instance_key` if the process
   has exhausted retries. A barrier timeout indicates that the worker kill or
   release did not occur. Missing `connection_dropped` or `committed_delay` events
   indicate that the worker has not reached the finance proxy.
5. Restore a stopped worker with
   `docker compose --project-name invoice-recovery -f compose.yaml -f compose.recovery.yaml start invoice-workers`.
   Rerun the scenario after resolving the failure. Each run creates new invoice IDs.

Stop this project's services with `docker compose --project-name invoice-recovery
-f compose.yaml -f compose.recovery.yaml stop`. Keep the volumes to retain evidence.
The proxy control endpoints are local test controls and belong only in this
recovery override. The fault schedule exercises transient single-request network
outages; longer outages and exhausted-retry incident resolution remain covered by
the existing `scripts/recovery_demo.py` flow. The harness reports observations from
the local stack and does not establish production capacity.
