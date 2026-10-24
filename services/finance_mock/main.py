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
        reference = f"FIN-{uuid.uuid4().hex[:12].upper()}"
        inserted = conn.execute(
                """INSERT INTO finance_postings (invoice_id, idempotency_key, external_reference, response_payload)
                   VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING
                   RETURNING external_reference""",
                (posting.invoice_id, idempotency_key, reference, json_value({"external_reference": reference})),
        ).fetchone()
        if not inserted:
            row = conn.execute(
                "SELECT invoice_id, external_reference FROM finance_postings "
                "WHERE idempotency_key = %s OR invoice_id = %s",
                (idempotency_key, posting.invoice_id),
            ).fetchall()
            if len(row) != 1 or row[0]["invoice_id"] != posting.invoice_id:
                raise HTTPException(status_code=409, detail="idempotency key belongs to another invoice")
            return {"external_reference": row[0]["external_reference"], "reused": True}
        conn.commit()
    return {"external_reference": reference, "reused": False}
