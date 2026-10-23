import json
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.db import audit, connection, json_value


CAMUNDA_REST_URL = os.getenv("CAMUNDA_REST_URL", "http://orchestration:8080/v2")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.80"))
app = FastAPI(title="Invoice workflow intake", openapi_url="/api/v1/openapi.json")


def versioned_openapi():
    contract = Path(__file__).resolve().parents[2] / "contracts/v1/openapi.json"
    return json.loads(contract.read_text())


app.openapi = versioned_openapi


class InvoiceIn(BaseModel):
    model_config = ConfigDict(json_encoders={Decimal: str})
    idempotency_key: str = Field(min_length=1, max_length=255)
    supplier_code: str
    purchase_order_number: str
    supplier_invoice_number: str
    invoice_date: date
    currency: str = Field(min_length=3, max_length=3)
    net_amount: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    line_notes: list[str] = []

    @model_validator(mode="after")
    def totals_match(self):
        if self.net_amount + self.tax_amount != self.total_amount:
            raise ValueError("total_amount must equal net_amount + tax_amount")
        return self


@app.get("/health")
def health():
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/invoices", status_code=status.HTTP_202_ACCEPTED)
def receive_invoice(invoice: InvoiceIn, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if idempotency_key and idempotency_key != invoice.idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header must match body")
    payload = invoice.model_dump(mode="json")
    with connection() as conn:
        existing = conn.execute(
            "SELECT invoice_id, status FROM invoices WHERE idempotency_key = %s", (invoice.idempotency_key,)
        ).fetchone()
        if existing:
            process_key = conn.execute(
                "SELECT process_instance_key FROM process_metrics WHERE invoice_id = %s", (existing["invoice_id"],)
            ).fetchone()
            return {"invoice_id": str(existing["invoice_id"]), "process_instance_key": process_key["process_instance_key"] if process_key else None, "reused": True, "status": existing["status"]}
        supplier = conn.execute("SELECT supplier_id FROM suppliers WHERE supplier_code = %s", (invoice.supplier_code,)).fetchone()
        po = conn.execute("SELECT purchase_order_id FROM purchase_orders WHERE po_number = %s", (invoice.purchase_order_number,)).fetchone()
        invoice_id = uuid.uuid4()
        try:
            conn.execute(
                """INSERT INTO invoices (invoice_id, idempotency_key, supplier_id, purchase_order_id,
                   supplier_invoice_number, invoice_date, currency, net_amount, tax_amount, total_amount, raw_payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                (invoice_id, invoice.idempotency_key, supplier["supplier_id"] if supplier else None,
                 po["purchase_order_id"] if po else None, invoice.supplier_invoice_number, invoice.invoice_date,
                 invoice.currency.upper(), invoice.net_amount, invoice.tax_amount, invoice.total_amount, json_value(payload)),
            )
        except Exception as exc:
            if "invoices_supplier_id_supplier_invoice_number_key" in str(exc):
                raise HTTPException(status_code=409, detail="supplier invoice number already received") from exc
            raise
        audit(conn, invoice_id, "INVOICE_RECEIVED", details={"idempotency_key": invoice.idempotency_key})
        conn.execute("INSERT INTO process_metrics (invoice_id, started_at) VALUES (%s, now())", (invoice_id,))
        conn.commit()
    try:
        response = httpx.post(
            f"{CAMUNDA_REST_URL}/process-instances",
            json={"processDefinitionId": "invoice-approval", "variables": {"invoice_id": str(invoice_id), "confidence_threshold": CONFIDENCE_THRESHOLD}},
            timeout=10,
        )
        response.raise_for_status()
        process_key = response.json().get("processInstanceKey")
        if process_key is None:
            raise RuntimeError("Camunda response did not include processInstanceKey")
        with connection() as conn:
            conn.execute("UPDATE process_metrics SET process_instance_key = %s WHERE invoice_id = %s", (process_key, invoice_id))
            audit(conn, invoice_id, "PROCESS_STARTED", details={"process_instance_key": process_key}, process_instance_key=process_key)
            conn.commit()
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE invoices SET status = 'TECHNICAL_FAILURE', updated_at = now() WHERE invoice_id = %s", (invoice_id,))
            audit(conn, invoice_id, "PROCESS_START_FAILED", details={"error": str(exc)})
            conn.commit()
        raise HTTPException(status_code=503, detail="invoice persisted but workflow could not start") from exc
    return {"invoice_id": str(invoice_id), "process_instance_key": process_key, "reused": False, "status": "RECEIVED"}
