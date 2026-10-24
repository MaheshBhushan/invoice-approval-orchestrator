"""OCR intake for scanned PDF invoices.

Tesseract reads the rasterised pages, an extractor maps the text onto the invoice
schema, and deterministic checks decide whether the extraction is trustworthy
enough for straight-through processing or must go to human review.
"""
import json
import os
import re
import subprocess
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx


OCR_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.80"))
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
EXTRACTION_MODEL = os.getenv("OCR_EXTRACTION_MODEL", "claude-haiku-5-5")
ANTHROPIC_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"

FIELDS = (
    "supplier_code",
    "purchase_order_number",
    "supplier_invoice_number",
    "invoice_date",
    "currency",
    "net_amount",
    "tax_amount",
    "total_amount",
)

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_code": {"type": ["string", "null"], "description": "Supplier code such as SUP-001"},
        "purchase_order_number": {"type": ["string", "null"], "description": "Purchase order number such as PO-1001"},
        "supplier_invoice_number": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"], "description": "ISO 8601 date"},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code"},
        "net_amount": {"type": ["string", "null"], "description": "Decimal string without currency symbol"},
        "tax_amount": {"type": ["string", "null"]},
        "total_amount": {"type": ["string", "null"]},
        "line_notes": {"type": "array", "items": {"type": "string"}},
        "field_confidence": {
            "type": "object",
            "description": "Confidence from 0 to 1 per extracted field",
            "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
    "required": [*FIELDS, "line_notes", "field_confidence"],
}


def ocr_pdf(pdf: bytes, dpi: int = OCR_DPI) -> dict:
    """Rasterise a PDF with pdftoppm and read it with Tesseract; confidence is the mean word confidence."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "invoice.pdf"
        source.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", str(source), str(Path(tmp) / "page")],
                       check=True, capture_output=True)
        pages = sorted(Path(tmp).glob("page*.png"))
        if not pages:
            raise ValueError("PDF contains no pages")
        lines, confidences = [], []
        for page in pages:
            tsv = subprocess.run(["tesseract", str(page), "-", "--psm", "6", "tsv"],
                                 check=True, capture_output=True, text=True).stdout
            current, words = None, []
            for row in tsv.splitlines()[1:]:
                cols = row.split("\t")
                if len(cols) < 12 or not cols[11].strip():
                    continue
                key = tuple(cols[1:5])
                if key != current and words:
                    lines.append(" ".join(words))
                    words = []
                current = key
                words.append(cols[11])
                if float(cols[10]) >= 0:
                    confidences.append(float(cols[10]) / 100)
            if words:
                lines.append(" ".join(words))
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return {"text": "\n".join(lines), "confidence": round(confidence, 3), "pages": len(pages)}


class LLMExtractor:
    """Schema-constrained extraction through a forced Claude tool call."""

    name = "llm"

    def __init__(self, api_key: str, model: str = EXTRACTION_MODEL, client: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.client = client or httpx.Client(timeout=30)

    def extract(self, text: str) -> dict:
        response = self.client.post(
            ANTHROPIC_URL,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": self.model,
                "max_tokens": 1024,
                "tools": [{"name": "record_invoice", "description": "Record fields read from an invoice.",
                           "input_schema": INVOICE_SCHEMA}],
                "tool_choice": {"type": "tool", "name": "record_invoice"},
                "messages": [{"role": "user", "content": (
                    "Extract the invoice fields from this OCR text. Use null for fields that are absent or "
                    "unreadable and lower field_confidence where OCR errors are likely.\n\n<ocr>\n"
                    f"{text}\n</ocr>")}],
            },
        )
        response.raise_for_status()
        for block in response.json()["content"]:
            if block.get("type") == "tool_use":
                return block["input"]
        raise ValueError("extractor returned no structured output")


class RuleExtractor:
    """Offline fallback that reads labelled fields with regular expressions."""

    name = "rules"
    PATTERNS = {
        "supplier_code": r"supplier(?: code)?\s*[:;]?\s*(SUP-?[0-9OIl]{3})",
        "purchase_order_number": r"(?:po|purchase order)(?: number| no\.?)?\s*[:;]?\s*(PO-?[0-9OIl]{4})",
        "supplier_invoice_number": r"invoice (?:number|no\.?)\s*[:;]?\s*([A-Z0-9][A-Z0-9-]+)",
        "invoice_date": r"invoice date\s*[:;]?\s*(\d{4}-\d{2}-\d{2})",
        "currency": r"currency\s*[:;]?\s*([A-Z]{3})",
        "net_amount": r"net(?: amount)?\s*[:;]?\s*([0-9][0-9,]*\.\d{2})",
        "tax_amount": r"(?:tax|vat)(?: amount)?\s*[:;]?\s*([0-9][0-9,]*\.\d{2})",
        "total_amount": r"total(?: amount)?\s*[:;]?\s*([0-9][0-9,]*\.\d{2})",
    }

    def extract(self, text: str) -> dict:
        result, confidence = {"line_notes": []}, {}
        for field, pattern in self.PATTERNS.items():
            match = re.search(pattern, text, re.IGNORECASE)
            value = match.group(1) if match else None
            if value and field in {"supplier_code", "purchase_order_number"}:
                prefix, _, digits = value.upper().partition("-") if "-" in value else (value[:-4], "", value[-4:])
                prefix = re.sub(r"[^A-Z]", "", prefix)
                value = f"{prefix}-{digits.replace('O', '0').replace('I', '1').replace('L', '1')}"
            result[field] = value.replace(",", "") if value and field.endswith("_amount") else value
            confidence[field] = 0.9 if value else 0.0
        result["line_notes"] = [m.strip() for m in re.findall(r"note\s*[:;]\s*(.+)", text, re.IGNORECASE)]
        result["field_confidence"] = confidence
        return result


def default_extractor():
    key = os.getenv("ANTHROPIC_API_KEY")
    return LLMExtractor(key) if key else RuleExtractor()


def validate(fields: dict) -> list[str]:
    issues = [f"missing {field}" for field in FIELDS if not fields.get(field)]
    amounts = {}
    for field in ("net_amount", "tax_amount", "total_amount"):
        try:
            amounts[field] = Decimal(str(fields[field])) if fields.get(field) else None
        except InvalidOperation:
            issues.append(f"unparseable {field}")
    if None not in amounts.values() and len(amounts) == 3 and amounts["net_amount"] + amounts["tax_amount"] != amounts["total_amount"]:
        issues.append("net_amount + tax_amount does not equal total_amount")
    if fields.get("invoice_date"):
        try:
            date.fromisoformat(fields["invoice_date"])
        except ValueError:
            issues.append("unparseable invoice_date")
    if fields.get("currency") and not re.fullmatch(r"[A-Z]{3}", fields["currency"]):
        issues.append("invalid currency")
    return issues


def intake(pdf: bytes, extractor=None, threshold: float = OCR_CONFIDENCE_THRESHOLD) -> dict:
    """Run OCR and extraction; return the invoice payload plus confidence metadata for routing."""
    extractor = extractor or default_extractor()
    ocr = ocr_pdf(pdf)
    fields = extractor.extract(ocr["text"])
    field_confidence = fields.get("field_confidence") or {}
    extraction_confidence = min((float(field_confidence.get(f, 0)) for f in FIELDS), default=0.0)
    issues = validate(fields)
    confidence = 0.0 if issues else round(min(ocr["confidence"], extraction_confidence), 3)
    payload = {field: fields.get(field) for field in FIELDS}
    payload["line_notes"] = [note for note in fields.get("line_notes") or [] if note]
    return {
        "payload": payload,
        "extraction": {
            "extractor": extractor.name,
            "ocr_confidence": ocr["confidence"],
            "extraction_confidence": round(extraction_confidence, 3),
            "confidence": confidence,
            "issues": issues,
            "pages": ocr["pages"],
            "requires_human_review": bool(issues) or confidence < threshold,
        },
        "text": ocr["text"],
    }


def render_invoice_pdf(values: dict, noise: float = 0.0, seed: int = 0) -> bytes:
    """Render invoice values as a scanned-looking single-page PDF (used by tests and the benchmark)."""
    import io
    import random

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    image = Image.new("L", (1700, 2200), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    lines = [
        "INVOICE",
        f"Supplier Code: {values['supplier_code']}",
        f"PO Number: {values['purchase_order_number']}",
        f"Invoice Number: {values['supplier_invoice_number']}",
        f"Invoice Date: {values['invoice_date']}",
        f"Currency: {values['currency']}",
        f"Net Amount: {float(values['net_amount']):.2f}",
        f"Tax Amount: {float(values['tax_amount']):.2f}",
        f"Total Amount: {float(values['total_amount']):.2f}",
        *[f"Note: {note}" for note in values.get("line_notes", [])],
    ]
    for index, line in enumerate(lines):
        draw.text((150, 150 + index * 90), line, fill=0, font=font)
    if noise:
        rng = random.Random(seed)
        pixels = image.load()
        for _ in range(int(image.width * image.height * noise)):
            pixels[rng.randrange(image.width), rng.randrange(image.height)] = rng.choice((0, 255))
        image = image.filter(ImageFilter.GaussianBlur(noise * 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=200)
    return buffer.getvalue()


if __name__ == "__main__":
    import sys

    print(json.dumps(intake(Path(sys.argv[1]).read_bytes()), indent=2))
