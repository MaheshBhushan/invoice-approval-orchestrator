# Power Automate human approval

The adapter connects a scheduled Power Automate cloud flow to Camunda's existing human task. An ambiguous synthetic invoice stays in `PENDING_APPROVAL` until a reviewer responds. Approval resumes the BPMN finance update. Rejection follows the existing rejection branch and creates no finance posting.

`power-automate/invoice-review.definition.json` contains the flow definition and expressions. It is a reference definition, not a tenant-exported solution ZIP. Create the cloud flow with the steps below and bind your own Approvals connection. Microsoft cloud execution has not been verified by the local test suite.

## Enable the adapter

Set `APPROVAL_API_TOKEN` in your protected `.env` file to a random token with at least 32 bytes of entropy. The adapter returns 503 when the token is unset. Both endpoints require `Authorization: Bearer <token>`.

Start the application services:

```sh
docker compose up -d --build postgres orchestration invoice-api finance-service invoice-workers
```

Apply the migration to an existing database without resetting volumes:

```sh
docker compose exec -T postgres psql -U invoice_app -d invoice_workflow -v ON_ERROR_STOP=1 < db/init/005_approval_responses.sql
```

Fresh Compose and Helm databases load the migration automatically. Helm deployments can set `approvals.existingSecret` to a Secret containing a `token` key. Existing Helm databases need the same SQL migration.

Deploy the BPMN process and form with `scripts/demo.sh` or the test runner below. Use an HTTPS API URL that the Microsoft cloud can reach. A cloud flow cannot call `localhost` on your laptop. This repository does not publish an endpoint or provision a Microsoft tenant. Keep Camunda, the finance service, and the unauthenticated invoice intake private; expose only the `/approvals/` routes through your authenticated deployment.

## Create one scheduled cloud flow

Microsoft documents the [approval action](https://learn.microsoft.com/en-us/power-automate/modern-approvals) and the [Approvals connector](https://learn.microsoft.com/en-us/connectors/approvals/).

1. Create a **Scheduled cloud flow** that runs every minute. Set trigger concurrency to **1**. Use one enabled flow for this adapter.
2. Add an HTTP GET action named **Pending**. Use `<ApiBaseUrl>/approvals/pending` and the bearer header. Enable **Secure Inputs** and **Secure Outputs**. The response contains an `items` array with at most one ready invoice.
3. Add **Apply to each**, named **Review_each**, over `body('Pending')?['items']`. Keep its concurrency at **1**.
4. Inside the loop, add **Start and wait for an approval**, named **Human_approval**. Select **Approve/Reject – First to respond**. Bind your Approvals connection and assign your reviewer email. Set the title and details from the expressions in the definition. Include the invoice number, UUID, amount, currency, and AI explanation.
5. Add an HTTP POST action named **Record_response** after the approval succeeds. Use `<ApiBaseUrl>/approvals/<invoice_id>/response`, the bearer header, and `Content-Type: application/json`. Set its body using these expressions:

| Field | Expression |
| --- | --- |
| `task_key` | `items('Review_each')?['task_key']` |
| `decision` | `if(equals(outputs('Human_approval')?['body/outcome'], 'Approve'), 'approved', 'rejected')` |
| `reviewer_id` | `first(outputs('Human_approval')?['body/responses'])?['responder']?['email']` |
| `reason` | `coalesce(first(outputs('Human_approval')?['body/responses'])?['comments'], '')` |

6. Enable Secure Inputs and Secure Outputs on the callback. Set exponential retry with 8 retries, starting at 10 seconds and capped at 1 minute. Save the flow and run a synthetic approval and rejection. Check the run history for successful callback status 200.

Use secure configuration for `ApiToken`; do not paste a real token into the checked-in definition. The definition's `ApiBaseUrl`, `ApiToken`, and `ReviewerEmail` parameters describe your deployment configuration. When building the flow in the designer, replace those parameter references with your configured values. HTTP action access depends on your tenant's licensing and policies.

## Callback contract and recovery

```json
{
  "task_key": "2251799813686789",
  "decision": "rejected",
  "reviewer_id": "reviewer@example.test",
  "reason": "Synthetic invoice surcharge was not authorized"
}
```

Camunda keys remain strings to preserve their full integer precision. The adapter verifies that the task belongs to the invoice's active process. PostgreSQL persists the first response and its audit event before task completion. A matching duplicate returns `reused: true` and creates no second task completion. A changed decision, reviewer, task, or comment returns 409.

If delivery fails, the API returns 503 and retains the original response. Retry the exact callback body. Pending discovery excludes invoices with a recorded response, so a failed callback must be retried from its existing flow run. If retries exhaust, retain that body from the secured run and replay it through the same API. Do not create a new approval decision. A completed Camunda task is reconciled against the worker's recorded reviewer decision before delivery is marked complete. Eventual consistency can cause a temporary 503 during recovery.

Use Power Automate as the review interface for these cases. Completing the same task in Tasklist can race the cloud reviewer. Keep one flow enabled and concurrency at one to avoid duplicate approval cards. If a flow is canceled before its callback, the next run may create another card; the callback and finance update still accept only the first recorded decision. This demo processes one review at a time and does not implement leases for parallel reviewers.

## Reproduce the local tests

```sh
python -u scripts/test-power-automate.py
```

The runner generates an ephemeral token in memory, starts the services, applies the migration, deploys the process and form, and runs all tests against PostgreSQL, Camunda, workers, and the Spring Boot finance service. It prints each phase. The token is not written to the repository. The API container retains the temporary test configuration; restart `invoice-api` with your configured `.env` token before connecting the cloud flow.

The new tests verify:

- Approval produces one finance posting and records the reviewer.
- Rejection records the reviewer and reason and produces zero postings.
- Concurrent duplicate responses and later replays produce one response audit event and one posting at most.
- Conflicting responses, wrong task IDs, invalid decisions, blank reviewer identities, and missing or malformed authentication cannot resume the invoice.
- Failed delivery, a lost completion reply, and a transitioning Camunda task preserve the decision and recover through an exact retry.

These tests exercise the cloud callback contract locally. Complete the two real reviewer responses in your Microsoft tenant to establish Power Automate execution evidence. Save the flow run IDs and inspect the `APPROVAL_RESPONSE_RECORDED`, `APPROVAL_RESPONSE_DELIVERED`, and `INVOICE_APPROVED` or `INVOICE_REJECTED` audit events for each invoice. Power BI is outside this integration.
