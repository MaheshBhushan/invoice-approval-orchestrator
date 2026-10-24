import json
import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://invoice_app:invoice_dev@postgres:5432/invoice_workflow"
)


@contextmanager
def connection():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def json_value(value):
    return json.dumps(value, default=str)


def audit(conn, invoice_id, event_type, actor_type="SYSTEM", actor_id=None, details=None, process_instance_key=None):
    conn.execute(
        """INSERT INTO audit_events
           (invoice_id, process_instance_key, event_type, actor_type, actor_id, details)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
        (invoice_id, process_instance_key, event_type, actor_type, actor_id, json_value(details or {})),
    )


def begin_attempt(conn, invoice_id, step, process_instance_key=None):
    # Serialize attempt allocation across workers for this invoice.
    conn.execute("SELECT invoice_id FROM invoices WHERE invoice_id = %s FOR UPDATE", (invoice_id,))
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number "
        "FROM processing_attempts WHERE invoice_id = %s AND step = %s",
        (invoice_id, step),
    ).fetchone()
    attempt_number = row["attempt_number"]
    conn.execute(
        """INSERT INTO processing_attempts (invoice_id, process_instance_key, step, attempt_number, outcome)
           VALUES (%s, %s, %s, %s, 'STARTED')""",
        (invoice_id, process_instance_key, step, attempt_number),
    )
    return attempt_number


def finish_attempt(conn, invoice_id, step, attempt_number, outcome, error_code=None, error_message=None):
    conn.execute(
        """UPDATE processing_attempts SET outcome = %s, error_code = %s, error_message = %s,
           finished_at = now() WHERE invoice_id = %s AND step = %s AND attempt_number = %s""",
        (outcome, error_code, error_message, invoice_id, step, attempt_number),
    )
    if outcome == "RETRYABLE_FAILURE":
        conn.execute("UPDATE process_metrics SET retry_count = retry_count + 1 WHERE invoice_id = %s", (invoice_id,))
