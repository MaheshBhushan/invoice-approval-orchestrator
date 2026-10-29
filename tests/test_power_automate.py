"""Complete synthetic invoices through the Power Automate callback contract."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import API_URL, db_one, payload, wait_for_invoice, wait_for_task
from test_workflow import submit

TOKEN = os.getenv("APPROVAL_API_TOKEN", "")
pytestmark = pytest.mark.skipif(not TOKEN, reason="Run scripts/test-power-automate.py to enable approval adapter")


def headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def review(client):
    created = submit(client, payload("ambiguous"))
    wait_for_invoice(created["invoice_id"], {"PENDING_APPROVAL"})
    task = wait_for_task(client, created["process_instance_key"])
    return created["invoice_id"], str(task["userTaskKey"])


@pytest.mark.parametrize("decision,terminal,postings", [("approved", "POSTED", 1), ("rejected", "REJECTED", 0)])
def test_response_and_concurrent_duplicate(client, decision, terminal, postings):
    invoice_id, task_key = review(client)
    body = {"task_key": task_key, "decision": decision,
            "reviewer_id": "synthetic-reviewer@example.test", "reason": "Synthetic approval test"}
    url = f"{API_URL}/approvals/{invoice_id}/response"
    pending = client.get(f"{API_URL}/approvals/pending", headers=headers())
    assert pending.status_code == 200, pending.text
    assert any(item["invoice_id"] == invoice_id for item in pending.json()["items"])
    assert client.post(url, json=body).status_code == 401
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=body, headers=headers()), range(2)))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert sorted(r.json()["reused"] for r in responses) == [False, True]
    wait_for_invoice(invoice_id, {terminal})
    assert client.post(url, json=body, headers=headers()).json()["reused"] is True
    conflicting = {**body, "decision": "rejected" if decision == "approved" else "approved"}
    assert client.post(url, json=conflicting, headers=headers()).status_code == 409
    assert db_one("SELECT count(*) AS count FROM finance_postings WHERE invoice_id = %s", (invoice_id,))["count"] == postings
    assert db_one("SELECT count(*) AS count FROM audit_events WHERE invoice_id = %s AND event_type = 'APPROVAL_RESPONSE_RECORDED'", (invoice_id,))["count"] == 1
    event_type = "INVOICE_APPROVED" if decision == "approved" else "INVOICE_REJECTED"
    event = db_one("SELECT actor_id, details FROM audit_events WHERE invoice_id = %s AND event_type = %s", (invoice_id, event_type))
    assert event["actor_id"] == body["reviewer_id"]
    assert event["details"]["human_decision"] == decision
    if decision == "rejected":
        assert event["details"]["rejection_reason"] == body["reason"]


def test_wrong_task_and_invalid_decision_cannot_resume_invoice(client):
    invoice_id, task_key = review(client)
    body = {"task_key": "1", "decision": "approved", "reviewer_id": "synthetic-reviewer"}
    url = f"{API_URL}/approvals/{invoice_id}/response"
    assert client.post(url, json=body, headers=headers()).status_code == 409
    assert client.post(url, json={**body, "task_key": task_key, "decision": "maybe"}, headers=headers()).status_code == 422
    assert db_one("SELECT count(*) AS count FROM approval_responses WHERE invoice_id = %s", (invoice_id,))["count"] == 0
    # Finish the synthetic case so subsequent scheduled reviews have no leftovers.
    result = client.post(url, json={**body, "task_key": task_key, "decision": "rejected"}, headers=headers())
    assert result.status_code == 200, result.text
    wait_for_invoice(invoice_id, {"REJECTED"})


@pytest.mark.parametrize("lost_success", [False, True])
def test_delivery_failure_preserves_decision_and_recovers(client, monkeypatch, lost_success):
    import uuid

    import httpx
    from api import approvals
    from fastapi import HTTPException

    invoice_id, task_key = review(client)
    body = {"task_key": task_key, "decision": "approved",
            "reviewer_id": "recovery-reviewer@example.test", "reason": "Recovery test"}
    original_post = approvals.httpx.post

    def interrupted_post(url, **kwargs):
        if url.endswith("/completion"):
            if lost_success:
                result = original_post(url, **kwargs)
                result.raise_for_status()
            raise httpx.ReadTimeout("Synthetic dropped completion response")
        return original_post(url, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(approvals.httpx, "post", interrupted_post)
        with pytest.raises(HTTPException) as failure:
            approvals.respond(uuid.UUID(invoice_id), approvals.ApprovalResponse(**body))
        assert failure.value.status_code == 503
    stored = db_one("SELECT response, delivered FROM approval_responses WHERE invoice_id = %s", (invoice_id,))
    assert stored["response"] == body
    assert stored["delivered"] is False
    with monkeypatch.context() as context:
        context.setattr(approvals.httpx, "get", lambda url, **kwargs: httpx.Response(
            200, json={"state": "COMPLETING"}, request=httpx.Request("GET", url)))
        with pytest.raises(HTTPException) as transitioning:
            approvals.respond(uuid.UUID(invoice_id), approvals.ApprovalResponse(**body))
        assert transitioning.value.status_code == 503
    url = f"{API_URL}/approvals/{invoice_id}/response"
    assert client.post(url, json={**body, "decision": "rejected"}, headers=headers()).status_code == 409
    if lost_success:
        wait_for_invoice(invoice_id, {"POSTED"})
        # Wait for Camunda's eventually consistent task index before replaying.
        import time
        for _ in range(50):
            task = client.get(f"{approvals.camunda_url()}/user-tasks/{task_key}")
            if task.json().get("state") == "COMPLETED":
                break
            time.sleep(0.2)
        else:
            pytest.fail("Completed task did not appear in Camunda index")
    recovered = client.post(url, json=body, headers=headers())
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["reused"] is True
    wait_for_invoice(invoice_id, {"POSTED"})
    assert db_one("SELECT count(*) AS count FROM finance_postings WHERE invoice_id = %s", (invoice_id,))["count"] == 1
    assert db_one("SELECT count(*) AS count FROM audit_events WHERE invoice_id = %s AND event_type = 'APPROVAL_RESPONSE_RECORDED'", (invoice_id,))["count"] == 1
