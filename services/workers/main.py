import asyncio
import os

import httpx
from pyzeebe import ZeebeWorker, create_insecure_channel
from pyzeebe.errors import BusinessError

from common.classifier import GRAPH
from common.db import audit, begin_attempt, connection, finish_attempt


ZEEBE_HOST = os.getenv("ZEEBE_HOST", "orchestration")
ZEEBE_PORT = int(os.getenv("ZEEBE_PORT", "26500"))
FINANCE_URL = os.getenv("FINANCE_URL", "http://finance-mock:8001")
FINANCE_TIMEOUT_SECONDS = float(os.getenv("FINANCE_TIMEOUT_SECONDS", "10"))
FINANCE_JOB_TIMEOUT_MS = int(os.getenv("FINANCE_JOB_TIMEOUT_MS", "10000"))
HUMAN_REVIEW_WEBHOOK_URL = os.getenv("HUMAN_REVIEW_WEBHOOK_URL")


def invoice_row(conn, invoice_id):
    row = conn.execute("SELECT * FROM invoices WHERE invoice_id = %s", (invoice_id,)).fetchone()
    if not row:
        raise ValueError(f"invoice {invoice_id} was not found")
    return row


def record_retryable(invoice_id, step, exc, attempt=None):
    with connection() as conn:
        if attempt is None:
            attempt = begin_attempt(conn, invoice_id, step)
        finish_attempt(conn, invoice_id, step, attempt, "RETRYABLE_FAILURE", "TECHNICAL_FAILURE", str(exc))
        audit(conn, invoice_id, "STEP_RETRYABLE_FAILURE", details={"step": step, "error": str(exc)})
        conn.commit()


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
channel = create_insecure_channel(f"{ZEEBE_HOST}:{ZEEBE_PORT}")
worker = ZeebeWorker(channel)


@worker.task(task_type="invoice.extract")
async def extract(invoice_id: str):
    step = "invoice.extract"
    try:
        with connection() as conn:
            attempt = begin_attempt(conn, invoice_id, step)
            row = invoice_row(conn, invoice_id)
            conn.execute("UPDATE invoices SET status = 'EXTRACTED', updated_at = now() WHERE invoice_id = %s", (invoice_id,))
            finish_attempt(conn, invoice_id, step, attempt, "SUCCEEDED")
            audit(conn, invoice_id, "FIELDS_EXTRACTED", details={"source": "structured_demo_payload"})
            conn.commit()
        return {"invoice_extracted": True}
    except Exception as exc:
        record_retryable(invoice_id, step, exc)
        raise


@worker.task(task_type="invoice.validate")
async def validate(invoice_id: str):
    step = "invoice.validate"
    try:
        with connection() as conn:
            attempt = begin_attempt(conn, invoice_id, step)
            invoice = invoice_row(conn, invoice_id)
            payload = invoice["raw_payload"]
            supplier = conn.execute(
                "SELECT * FROM suppliers WHERE supplier_code = %s AND is_active = true", (payload["supplier_code"],)
            ).fetchone()
            po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = %s", (payload["purchase_order_number"],)).fetchone()
            error = None
            if not supplier:
                error = "unknown_supplier"
            elif not po:
                error = "unknown_purchase_order"
            elif po["supplier_id"] != supplier["supplier_id"]:
                error = "purchase_order_supplier_mismatch"
            elif po["status"] != "OPEN":
                error = "purchase_order_not_open"
            elif po["currency"] != invoice["currency"]:
                error = "currency_mismatch"
            if error:
                conn.execute("UPDATE invoices SET status = 'VALIDATION_FAILED', updated_at = now() WHERE invoice_id = %s", (invoice_id,))
                finish_attempt(conn, invoice_id, step, attempt, "BUSINESS_FAILURE", "VALIDATION_FAILED", error)
                audit(conn, invoice_id, "VALIDATION_FAILED", details={"classification": error})
                conn.commit()
                raise BusinessError("VALIDATION_FAILED", error)
            finish_attempt(conn, invoice_id, step, attempt, "SUCCEEDED")
            audit(conn, invoice_id, "VALIDATED", details={"supplier_code": payload["supplier_code"], "po_number": po["po_number"]})
            conn.commit()
        return {"validation_passed": True}
    except BusinessError:
        raise
    except Exception as exc:
        record_retryable(invoice_id, step, exc)
        raise


@worker.task(task_type="invoice.classify-discrepancy")
async def classify_discrepancy(invoice_id: str, confidence_threshold: float = 0.8):
    step = "invoice.classify-discrepancy"
    try:
        with connection() as conn:
            attempt = begin_attempt(conn, invoice_id, step)
            invoice = invoice_row(conn, invoice_id)
            payload = invoice["raw_payload"]
            po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = %s", (payload["purchase_order_number"],)).fetchone()
            decision = GRAPH.invoke({"invoice": payload, "purchase_order": po, "validation": {}}).get("decision")
            required = {"classification", "severity", "confidence", "recommended_action", "evidence", "explanation"}
            valid = isinstance(decision, dict) and required <= decision.keys() and isinstance(decision["confidence"], (float, int)) and 0 <= decision["confidence"] <= 1
            if not valid:
                decision = {
                    "classification": "invalid_agent_output", "severity": "unknown", "confidence": 0.0,
                    "recommended_action": "human_review", "evidence": [],
                    "explanation": "The agent returned an invalid structured decision; human review is required.",
                }
            requires_human = decision["confidence"] < float(confidence_threshold) or decision["recommended_action"] == "human_review"
            conn.execute(
                """UPDATE invoices SET status = 'PENDING_APPROVAL', agent_classification = %s,
                   agent_confidence = %s, agent_explanation = %s, updated_at = now() WHERE invoice_id = %s""",
                (decision["classification"], decision["confidence"], decision["explanation"], invoice_id),
            )
            if not requires_human:
                conn.execute("UPDATE invoices SET status = 'CLASSIFIED' WHERE invoice_id = %s", (invoice_id,))
            finish_attempt(conn, invoice_id, step, attempt, "SUCCEEDED")
            audit(conn, invoice_id, "AGENT_CLASSIFIED", "AGENT", "langgraph", decision)
            conn.commit()
        if requires_human and HUMAN_REVIEW_WEBHOOK_URL:
            try:
                httpx.post(
                    HUMAN_REVIEW_WEBHOOK_URL,
                    json={"invoice_id": invoice_id, "supplier_code": payload["supplier_code"],
                          "purchase_order_number": payload["purchase_order_number"], "decision": decision},
                    timeout=10,
                ).raise_for_status()
                with connection() as conn:
                    audit(conn, invoice_id, "HUMAN_REVIEW_NOTIFICATION_SENT", details={"channel": "n8n"})
                    conn.commit()
            except Exception as exc:
                record_retryable(invoice_id, "invoice.notify-human-review", exc)
        return {
            "agent_confidence": decision["confidence"], "agent_requires_human": requires_human,
            "agent_decision": decision, "supplier_code": payload["supplier_code"],
            "purchase_order_number": payload["purchase_order_number"],
            "agent_evidence": "\n".join(decision["evidence"]),
            "agent_explanation": decision["explanation"],
        }
    except Exception as exc:
        record_retryable(invoice_id, step, exc)
        raise


@worker.task(task_type="invoice.escalate-review")
async def escalate_review(invoice_id: str):
    with connection() as conn:
        attempt = begin_attempt(conn, invoice_id, "invoice.escalate-review")
        finish_attempt(conn, invoice_id, "invoice.escalate-review", attempt, "SUCCEEDED")
        audit(conn, invoice_id, "HUMAN_REVIEW_ESCALATED", details={"reason": "review_overdue"})
        conn.commit()
    return {"review_escalated": True}


@worker.task(task_type="invoice.record-rejection")
async def record_rejection(invoice_id: str, human_decision: str | None = None, reviewer_id: str | None = None,
                           rejection_reason: str | None = None, validation_error: str | None = None):
    with connection() as conn:
        attempt = begin_attempt(conn, invoice_id, "invoice.record-rejection")
        conn.execute("UPDATE invoices SET status = 'REJECTED', updated_at = now() WHERE invoice_id = %s", (invoice_id,))
        finish_attempt(conn, invoice_id, "invoice.record-rejection", attempt, "SUCCEEDED")
        audit(conn, invoice_id, "INVOICE_REJECTED", "HUMAN" if human_decision else "SYSTEM", reviewer_id,
              {"human_decision": human_decision, "rejection_reason": rejection_reason,
               "validation_error": validation_error})
        conn.commit()
    return {"invoice_rejected": True}


@worker.task(task_type="invoice.update-finance", timeout_ms=FINANCE_JOB_TIMEOUT_MS)
async def update_finance(invoice_id: str, human_decision: str | None = None, reviewer_id: str | None = None):
    step = "invoice.update-finance"
    committed_attempt = None
    try:
        with connection() as conn:
            attempt = begin_attempt(conn, invoice_id, step)
            invoice = invoice_row(conn, invoice_id)
            conn.execute("UPDATE invoices SET status = 'POSTING', updated_at = now() WHERE invoice_id = %s AND status <> 'POSTED'", (invoice_id,))
            if human_decision == "approved":
                audit(conn, invoice_id, "INVOICE_APPROVED", "HUMAN", reviewer_id,
                      {"human_decision": human_decision})
            conn.commit()
            committed_attempt = attempt
        response = httpx.post(
            f"{FINANCE_URL}/postings",
            json={"invoice_id": invoice_id, "amount": float(invoice["total_amount"]), "currency": invoice["currency"]},
            headers={"Idempotency-Key": f"invoice:{invoice_id}"}, timeout=FINANCE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
        with connection() as conn:
            # Concurrent deliveries can all receive the reused posting; only the first records the transition.
            posted = conn.execute(
                "UPDATE invoices SET status = 'POSTED', updated_at = now() WHERE invoice_id = %s AND status <> 'POSTED' "
                "RETURNING invoice_id", (invoice_id,),
            ).fetchone()
            finish_attempt(conn, invoice_id, step, attempt, "SUCCEEDED")
            if posted:
                audit(conn, invoice_id, "FINANCE_POSTED", details=result)
            conn.commit()
        return {"finance_reference": result["external_reference"]}
    except Exception as exc:
        record_retryable(invoice_id, step, exc, committed_attempt)
        raise


@worker.task(task_type="invoice.store-metrics")
async def store_metrics(invoice_id: str, agent_requires_human: bool = False):
    with connection() as conn:
        attempt = begin_attempt(conn, invoice_id, "invoice.store-metrics")
        conn.execute(
            """UPDATE process_metrics SET completed_at = now(), straight_through = %s,
               human_review_count = CASE WHEN %s THEN GREATEST(human_review_count, 1) ELSE human_review_count END
               WHERE invoice_id = %s""",
            (not agent_requires_human, agent_requires_human, invoice_id),
        )
        finish_attempt(conn, invoice_id, "invoice.store-metrics", attempt, "SUCCEEDED")
        audit(conn, invoice_id, "PROCESS_COMPLETED", details={"straight_through": not agent_requires_human})
        conn.commit()
    return {"metrics_stored": True}


if __name__ == "__main__":
    loop.run_until_complete(worker.work())
