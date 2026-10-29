import os
import time
import uuid

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row


API_URL = os.getenv("API_URL", "http://invoice-api:8000")
CAMUNDA_URL = os.getenv("CAMUNDA_URL", "http://orchestration:8080")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://invoice_app:invoice_dev@postgres:5432/invoice_workflow"
)
FINANCE_URL = os.getenv("FINANCE_URL", "http://finance-service:8001")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(timeout=10) as value:
        yield value


def payload(case, *, suffix=None):
    suffix = suffix or uuid.uuid4().hex[:10]
    values = {
        "idempotency_key": f"test-{case}-{suffix}",
        "supplier_code": "SUP-001",
        "purchase_order_number": "PO-1001",
        "supplier_invoice_number": f"TEST-{case.upper()}-{suffix}",
        "invoice_date": "2026-08-21",
        "currency": "EUR",
        "net_amount": 840.34,
        "tax_amount": 159.66,
        "total_amount": 1000.00,
    }
    if case == "unknown":
        values["supplier_code"] = "SUP-999"
    elif case == "mismatch":
        values.update(net_amount=1000.00, tax_amount=190.00, total_amount=1190.00)
    elif case == "ambiguous":
        values.update(
            supplier_code="SUP-002",
            purchase_order_number="PO-2001",
            net_amount=2113.45,
            tax_amount=401.55,
            total_amount=2515.00,
            line_notes=["Service surcharge per verbal agreement"],
        )
    return values


def db_one(sql, params=()):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchone()


def wait_for_invoice(invoice_id, statuses, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = db_one(
            "SELECT * FROM invoices WHERE invoice_id = %s", (invoice_id,)
        )
        if row and row["status"] in statuses:
            return row
        time.sleep(0.2)
    raise AssertionError(f"invoice {invoice_id} did not reach {statuses} within {timeout}s")


def wait_for_task(client, process_instance_key, timeout=30):
    deadline = time.monotonic() + timeout
    body = {
        "filter": {
            "state": "CREATED",
            "processInstanceKey": str(process_instance_key),
        },
        "page": {"limit": 1},
    }
    while time.monotonic() < deadline:
        response = client.post(f"{CAMUNDA_URL}/v2/user-tasks/search", json=body)
        response.raise_for_status()
        if response.json()["items"]:
            return response.json()["items"][0]
        time.sleep(0.2)
    raise AssertionError(f"no user task for process {process_instance_key} within {timeout}s")
