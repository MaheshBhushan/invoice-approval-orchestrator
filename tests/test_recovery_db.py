"""PostgreSQL concurrency regression checks, enabled only in the recovery stack."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from common.db import begin_attempt, connection, finish_attempt
from fastapi import HTTPException
from finance_mock.main import PostingIn, create_posting


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


def test_concurrent_first_finance_deliveries_share_one_reference():
    invoice_id = new_invoice()
    posting = PostingIn(invoice_id=invoice_id, amount=1000, currency="EUR")
    key = f"invoice:{invoice_id}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: create_posting(posting, key), range(24)))
    assert len({row["external_reference"] for row in responses}) == 1
    assert sum(not row["reused"] for row in responses) == 1
    with connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM finance_postings WHERE invoice_id = %s",
                            (invoice_id,)).fetchone()["n"] == 1
    # An invoice-level duplicate with a new key also reuses the posting.
    assert create_posting(posting, f"retry:{invoice_id}")["external_reference"] == responses[0]["external_reference"]
    other = PostingIn(invoice_id=new_invoice(), amount=1000, currency="EUR")
    with pytest.raises(HTTPException) as error:
        create_posting(other, key)
    assert error.value.status_code == 409


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
