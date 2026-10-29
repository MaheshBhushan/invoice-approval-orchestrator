"""PostgreSQL concurrency regression checks, enabled only in the recovery stack.

Finance posting concurrency is covered by the finance-service JUnit suite."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from common.db import begin_attempt, connection, finish_attempt


pytestmark = pytest.mark.skipif(os.getenv("RECOVERY_DB_TEST") != "1", reason="requires isolated recovery PostgreSQL")


def new_invoice():
    invoice_id = uuid.uuid4()
    with connection() as conn:
        conn.execute(
            """INSERT INTO invoices
               (invoice_id, idempotency_key, supplier_invoice_number, invoice_date, currency,
                net_amount, tax_amount, total_amount, raw_payload)
               VALUES (%s, %s, %s, '2026-10-19', 'EUR', 1000, 0, 1000, '{}')""",
            (invoice_id, f"recovery-regression-{invoice_id}", str(invoice_id)),
        )
        conn.commit()
    return invoice_id


def test_attempt_allocation_is_serialized_across_connections():
    invoice_id = new_invoice()

    def attempt(_):
        with connection() as conn:
            number = begin_attempt(conn, invoice_id, "concurrent-regression")
            finish_attempt(conn, invoice_id, "concurrent-regression", number, "SUCCEEDED")
            conn.commit()
            return number

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(attempt, range(24)))
    assert sorted(numbers) == list(range(1, 25))
