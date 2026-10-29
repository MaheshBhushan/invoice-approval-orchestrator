"""Power Automate adapter for existing Camunda human review tasks."""
import hmac
import os
import uuid
from typing import Literal

import httpx
from common.db import audit, connection, json_value
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/approvals", tags=["human approval"])


def authenticate(authorization: str | None = Header(default=None)):
    token = os.getenv("APPROVAL_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "Power Automate approval adapter is disabled")
    if not hmac.compare_digest(
        (authorization or "").encode(), f"Bearer {token}".encode()
    ):
        raise HTTPException(401, "Invalid approval API token")


def camunda_url():
    return os.getenv("CAMUNDA_REST_URL", "http://orchestration:8080/v2")


def task_for_process(process_key):
    response = httpx.post(
        f"{camunda_url()}/user-tasks/search",
        json={"filter": {"state": "CREATED", "processInstanceKey": str(process_key)},
              "page": {"limit": 1}}, timeout=10,
    )
    response.raise_for_status()
    tasks = response.json()["items"]
    return tasks[0] if tasks else None


class ApprovalResponse(BaseModel):
    task_key: str = Field(pattern=r"^[0-9]+$", max_length=20)
    decision: Literal["approved", "rejected"]
    reviewer_id: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="", max_length=4000)

    @field_validator("reviewer_id")
    @classmethod
    def require_reviewer_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewer_id must identify a reviewer")
        return value


@router.get("/pending", dependencies=[Depends(authenticate)])
def pending():
    """Return one ready task so a serial scheduled flow cannot starve a batch."""
    try:
        with connection() as conn:
            rows = conn.execute(
                """SELECT i.invoice_id, i.supplier_invoice_number, i.total_amount,
                   i.currency, i.agent_explanation, m.process_instance_key
                   FROM invoices i JOIN process_metrics m USING (invoice_id)
                   LEFT JOIN approval_responses r USING (invoice_id)
                   WHERE i.status = 'PENDING_APPROVAL' AND r.invoice_id IS NULL
                   ORDER BY i.received_at LIMIT 20"""
            ).fetchall()
        for row in rows:
            task = task_for_process(row["process_instance_key"])
            if task:
                return {"items": [{"invoice_id": str(row["invoice_id"]),
                                   "task_key": str(task["userTaskKey"]),
                                   "invoice_number": row["supplier_invoice_number"],
                                   "amount": str(row["total_amount"]),
                                   "currency": row["currency"].strip(),
                                   "explanation": row["agent_explanation"]}]}
        return {"items": []}
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Camunda review tasks are temporarily unavailable") from exc


@router.post("/{invoice_id}/response", dependencies=[Depends(authenticate)])
def respond(invoice_id: uuid.UUID, response: ApprovalResponse):
    # Persist the first decision before contacting Camunda. A failed callback can
    # then retry without changing the decision, including after a process restart.
    with connection() as conn:
        invoice = conn.execute(
            "SELECT i.status, m.process_instance_key FROM invoices i "
            "JOIN process_metrics m USING (invoice_id) WHERE invoice_id = %s FOR UPDATE OF i",
            (invoice_id,),
        ).fetchone()
        if not invoice:
            raise HTTPException(404, "Invoice not found")
        existing = conn.execute(
            "SELECT * FROM approval_responses WHERE invoice_id = %s", (invoice_id,)
        ).fetchone()
        if existing:
            if existing["response"] != response.model_dump():
                raise HTTPException(409, "A different approval response is already recorded")
            if existing["delivered"]:
                return {"invoice_id": str(invoice_id), "decision": response.decision, "reused": True}
        else:
            if invoice["status"] != "PENDING_APPROVAL":
                raise HTTPException(409, "Invoice is not awaiting human approval")
            try:
                task = task_for_process(invoice["process_instance_key"])
            except httpx.HTTPError as exc:
                raise HTTPException(503, "Camunda review tasks are temporarily unavailable") from exc
            if not task or str(task["userTaskKey"]) != response.task_key:
                raise HTTPException(409, "Task does not match the invoice's active review")
            conn.execute(
                "INSERT INTO approval_responses (invoice_id, response) VALUES (%s, %s::jsonb)",
                (invoice_id, json_value(response.model_dump())),
            )
            audit(conn, invoice_id, "APPROVAL_RESPONSE_RECORDED", "HUMAN", response.reviewer_id,
                  {**response.model_dump(), "channel": "power-automate"})
        conn.commit()

    with connection() as conn:
        stored = conn.execute(
            "SELECT * FROM approval_responses WHERE invoice_id = %s FOR UPDATE", (invoice_id,)
        ).fetchone()
        if stored["delivered"]:
            return {"invoice_id": str(invoice_id), "decision": response.decision, "reused": True}
        try:
            # GET before completion also reconciles a lost successful HTTP reply.
            task_response = httpx.get(f"{camunda_url()}/user-tasks/{response.task_key}", timeout=10)
            task_response.raise_for_status()
            task = task_response.json()
            if task["state"] == "COMPLETED":
                event = conn.execute(
                    "SELECT actor_id, details FROM audit_events WHERE invoice_id = %s "
                    "AND event_type IN ('INVOICE_APPROVED', 'INVOICE_REJECTED') ORDER BY event_id LIMIT 1",
                    (invoice_id,),
                ).fetchone()
                if not event:
                    raise HTTPException(503, "Task completed; retry after the worker records the decision")
                if event["actor_id"] != response.reviewer_id or event["details"].get("human_decision") != response.decision:
                    raise HTTPException(409, "Task was completed with a different reviewer decision")
                if response.decision == "rejected" and event["details"].get("rejection_reason") != response.reason:
                    raise HTTPException(409, "Task was completed with a different rejection reason")
            else:
                if task["state"] in {"CREATING", "ASSIGNING", "UPDATING", "COMPLETING"}:
                    raise HTTPException(503, "Review task is transitioning; retry the same response")
                if task["state"] != "CREATED":
                    raise HTTPException(409, "Review task is no longer available")
                completed = httpx.post(
                    f"{camunda_url()}/user-tasks/{response.task_key}/completion",
                    json={"variables": {"human_decision": response.decision,
                                        "reviewer_id": response.reviewer_id,
                                        "rejection_reason": response.reason}}, timeout=10,
                )
                completed.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Decision recorded; retry this same response to resume delivery") from exc
        conn.execute("UPDATE approval_responses SET delivered = true WHERE invoice_id = %s", (invoice_id,))
        audit(conn, invoice_id, "APPROVAL_RESPONSE_DELIVERED", details={"task_key": response.task_key})
        conn.commit()
    return {"invoice_id": str(invoice_id), "decision": response.decision, "reused": existing is not None}
