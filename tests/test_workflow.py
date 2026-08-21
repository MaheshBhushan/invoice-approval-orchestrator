from conftest import (
    API_URL,
    CAMUNDA_URL,
    FINANCE_URL,
    db_one,
    payload,
    wait_for_invoice,
    wait_for_task,
)


def submit(client, invoice):
    response = client.post(
        f"{API_URL}/invoices",
        json=invoice,
        headers={"Idempotency-Key": invoice["idempotency_key"]},
    )
    assert response.status_code == 202, response.text
    return response.json()


def complete(client, task, decision):
    response = client.post(
        f"{CAMUNDA_URL}/v2/user-tasks/{task['userTaskKey']}/completion",
        json={
            "variables": {
                "human_decision": decision,
                "reviewer_id": "pytest-reviewer",
                "rejection_reason": "Rejected by deterministic integration test",
            }
        },
    )
    assert response.status_code == 204, response.text


def test_happy_path_and_duplicate_intake(client):
    invoice = payload("valid")
    first = submit(client, invoice)
    posted = wait_for_invoice(first["invoice_id"], {"POSTED"})

    second = submit(client, invoice)
    assert second["reused"] is True
    assert second["invoice_id"] == first["invoice_id"]
    assert str(second["process_instance_key"]) == str(first["process_instance_key"])
    assert db_one(
        "SELECT count(*) AS count FROM finance_postings WHERE invoice_id = %s",
        (first["invoice_id"],),
    )["count"] == 1
    assert posted["agent_classification"] == "no_discrepancy"


def test_invalid_supplier_is_terminal_business_rejection(client):
    created = submit(client, payload("unknown"))
    row = wait_for_invoice(created["invoice_id"], {"REJECTED"})
    assert row["status"] == "REJECTED"
    assert db_one(
        "SELECT count(*) AS count FROM finance_postings WHERE invoice_id = %s",
        (created["invoice_id"],),
    )["count"] == 0


def test_purchase_order_amount_mismatch_is_explained_and_posted(client):
    created = submit(client, payload("mismatch"))
    row = wait_for_invoice(created["invoice_id"], {"POSTED"})
    assert row["agent_classification"] == "amount_mismatch"
    assert float(row["agent_confidence"]) >= 0.8
    assert "190.00 EUR" in row["agent_explanation"]


def test_low_confidence_can_be_approved(client):
    created = submit(client, payload("ambiguous"))
    row = wait_for_invoice(created["invoice_id"], {"PENDING_APPROVAL"})
    assert float(row["agent_confidence"]) < 0.8
    task = wait_for_task(client, created["process_instance_key"])
    complete(client, task, "approved")
    wait_for_invoice(created["invoice_id"], {"POSTED"})
    metrics = db_one(
        "SELECT * FROM process_metrics WHERE invoice_id = %s", (created["invoice_id"],)
    )
    assert metrics["human_review_count"] == 1
    assert metrics["straight_through"] is False


def test_low_confidence_can_be_rejected(client):
    created = submit(client, payload("ambiguous"))
    wait_for_invoice(created["invoice_id"], {"PENDING_APPROVAL"})
    task = wait_for_task(client, created["process_instance_key"])
    complete(client, task, "rejected")
    wait_for_invoice(created["invoice_id"], {"REJECTED"})
    event = db_one(
        "SELECT actor_type, actor_id, details FROM audit_events "
        "WHERE invoice_id = %s AND event_type = 'INVOICE_REJECTED'",
        (created["invoice_id"],),
    )
    assert event["actor_type"] == "HUMAN"
    assert event["actor_id"] == "pytest-reviewer"
    assert event["details"]["rejection_reason"]


def test_duplicate_finance_delivery_returns_original_posting(client):
    created = submit(client, payload("valid"))
    row = wait_for_invoice(created["invoice_id"], {"POSTED"})
    body = {
        "invoice_id": str(row["invoice_id"]),
        "amount": float(row["total_amount"]),
        "currency": row["currency"],
    }
    response = client.post(
        f"{FINANCE_URL}/postings",
        json=body,
        headers={"Idempotency-Key": f"invoice:{row['invoice_id']}"},
    )
    assert response.status_code == 201
    assert response.json()["reused"] is True
    assert db_one(
        "SELECT count(*) AS count FROM finance_postings WHERE invoice_id = %s",
        (row["invoice_id"],),
    )["count"] == 1
