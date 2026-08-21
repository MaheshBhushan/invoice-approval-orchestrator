import uuid

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from common.db import connection, json_value


app = FastAPI(title="Idempotent demo finance system")


class PostingIn(BaseModel):
    invoice_id: uuid.UUID
    amount: float
    currency: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/postings", status_code=status.HTTP_201_CREATED)
def create_posting(posting: PostingIn, idempotency_key: str = Header(alias="Idempotency-Key")):
    with connection() as conn:
        existing = conn.execute("SELECT external_reference FROM finance_postings WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
        if existing:
            return {"external_reference": existing["external_reference"], "reused": True}
        reference = f"FIN-{uuid.uuid4().hex[:12].upper()}"
        try:
            conn.execute(
                """INSERT INTO finance_postings (invoice_id, idempotency_key, external_reference, response_payload)
                   VALUES (%s, %s, %s, %s::jsonb)""",
                (posting.invoice_id, idempotency_key, reference, json_value({"external_reference": reference})),
            )
            conn.commit()
        except Exception as exc:
            if "finance_postings_invoice_id_key" in str(exc):
                row = conn.execute("SELECT external_reference FROM finance_postings WHERE invoice_id = %s", (posting.invoice_id,)).fetchone()
                return {"external_reference": row["external_reference"], "reused": True}
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"external_reference": reference, "reused": False}
