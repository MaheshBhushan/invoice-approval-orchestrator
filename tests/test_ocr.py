import json

import httpx
from fastapi.testclient import TestClient

from api import main as api
from common import ocr
from common.classifier import GRAPH


INVOICE = {
    "supplier_code": "SUP-002",
    "purchase_order_number": "PO-2001",
    "supplier_invoice_number": "SCAN-2001-7",
    "invoice_date": "2026-08-21",
    "currency": "EUR",
    "net_amount": 2113.45,
    "tax_amount": 401.55,
    "total_amount": 2515.00,
    "line_notes": ["Service surcharge per verbal agreement"],
}


def test_clean_scan_extracts_schema_fields():
    result = ocr.intake(ocr.render_invoice_pdf(INVOICE), ocr.RuleExtractor())
    payload = result["payload"]
    assert payload["supplier_code"] == "SUP-002"
    assert payload["purchase_order_number"] == "PO-2001"
    assert payload["supplier_invoice_number"] == "SCAN-2001-7"
    assert payload["invoice_date"] == "2026-08-21"
    assert payload["total_amount"] == "2515.00"
    assert payload["line_notes"] == ["Service surcharge per verbal agreement"]
    assert result["extraction"]["issues"] == []
    assert result["extraction"]["ocr_confidence"] > 0.8
    assert not result["extraction"]["requires_human_review"]


def test_unreadable_scan_routes_to_human_review():
    result = ocr.intake(ocr.render_invoice_pdf(INVOICE, noise=0.12), ocr.RuleExtractor())
    extraction = result["extraction"]
    assert extraction["requires_human_review"]
    assert extraction["confidence"] == 0.0
    assert extraction["issues"]


def test_low_confidence_extraction_uses_human_review_route():
    result = ocr.intake(ocr.render_invoice_pdf({**INVOICE, "line_notes": []}), ocr.RuleExtractor(), threshold=0.99)
    assert result["extraction"]["requires_human_review"]
    invoice = {**result["payload"], "ocr_extraction": result["extraction"]}
    decision = GRAPH.invoke({"invoice": invoice, "purchase_order": {"total_amount": 2515.00}, "validation": {}})["decision"]
    assert decision["classification"] == "low_confidence_extraction"
    assert decision["recommended_action"] == "human_review"
    assert decision["evidence"][0].startswith("ocr.confidence=")


def test_llm_extractor_forces_schema_tool_call():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        fields = {key: str(INVOICE[key]) for key in ocr.FIELDS}
        body = {"content": [{"type": "tool_use", "name": "record_invoice", "input": {
            **fields, "line_notes": [], "field_confidence": {key: 0.6 for key in ocr.FIELDS}}}]}
        return httpx.Response(200, json=body)

    extractor = ocr.LLMExtractor("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    fields = extractor.extract("Supplier Code: SUP-002")
    assert seen["tool_choice"] == {"type": "tool", "name": "record_invoice"}
    assert seen["tools"][0]["input_schema"] == ocr.INVOICE_SCHEMA
    assert fields["supplier_code"] == "SUP-002"
    assert ocr.validate(fields) == []


def test_scan_endpoint_starts_workflow_with_extraction(monkeypatch):
    started = {}

    def fake_start(invoice, extraction=None):
        started.update(invoice=invoice, extraction=extraction)
        return {"invoice_id": "i-1", "process_instance_key": 1, "reused": False, "status": "RECEIVED"}

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(api, "start_invoice", fake_start)
    client = TestClient(api.app)
    pdf = ocr.render_invoice_pdf(INVOICE)
    response = client.post("/invoices/scan", content=pdf, headers={"Content-Type": "application/pdf"})
    assert response.status_code == 202, response.text
    assert started["invoice"].idempotency_key.startswith("scan-")
    assert str(started["invoice"].total_amount) == "2515.00"
    assert started["extraction"]["extractor"] in {"rules", "llm"}
    assert client.post("/invoices/scan", content=b"not a pdf").status_code == 415
