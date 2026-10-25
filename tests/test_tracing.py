"""Offline benchmark run that asserts one complete OpenTelemetry trace per invoice."""
import asyncio
import uuid
from collections import defaultdict

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from common import tracing
from common.classifier import GRAPH

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)

PO = {"total_amount": 1000.00}
CASES = {
    "no_discrepancy": {"total_amount": 1000.00, "currency": "EUR"},
    "amount_mismatch": {"total_amount": 1190.00, "currency": "EUR"},
    "ambiguous_discrepancy": {"total_amount": 1000.00, "currency": "EUR",
                              "line_notes": ["Service surcharge per verbal agreement"]},
}


@tracing.traced_step("invoice.extract")
async def extract(invoice_id):
    return {}


@tracing.traced_step("invoice.validate")
async def validate(invoice_id):
    return {"validation_passed": True}


@tracing.traced_step("invoice.classify-discrepancy")
async def classify_discrepancy(invoice_id, invoice=None, confidence_threshold=0.8):
    decision = GRAPH.invoke({"invoice": {**invoice, "invoice_id": invoice_id}, "purchase_order": PO})["decision"]
    return {"agent_confidence": decision["confidence"],
            "agent_requires_human": decision["confidence"] < confidence_threshold}


@tracing.traced_step("invoice.update-finance")
async def update_finance(invoice_id):
    return {"finance_reference": "FIN-1"}


@tracing.traced_step("invoice.store-metrics")
async def store_metrics(invoice_id):
    return {"metrics_stored": True}


async def process(invoice_id, invoice):
    await extract(invoice_id)
    await validate(invoice_id)
    await classify_discrepancy(invoice_id, invoice=invoice)
    await update_finance(invoice_id)
    await store_metrics(invoice_id)


def benchmark_case(index):
    bucket = index % 10
    return "no_discrepancy" if bucket < 7 else "amount_mismatch" if bucket < 9 else "ambiguous_discrepancy"


@pytest.fixture(autouse=True)
def clear_spans():
    EXPORTER.clear()


def spans_by_trace():
    grouped = defaultdict(list)
    for span in EXPORTER.get_finished_spans():
        grouped[format(span.context.trace_id, "032x")].append(span)
    return grouped


def test_benchmark_run_emits_complete_trace_per_invoice():
    invoices = {str(uuid.uuid4()): benchmark_case(i) for i in range(100)}

    async def run():
        for invoice_id, case in invoices.items():
            await process(invoice_id, CASES[case])

    asyncio.run(run())
    traces = spans_by_trace()
    assert set(traces) == {tracing.invoice_trace_id(i) for i in invoices}
    for invoice_id, case in invoices.items():
        spans = traces[tracing.invoice_trace_id(invoice_id)]
        assert tracing.missing_spans(s.name for s in spans) == [], invoice_id
        assert all(s.attributes["invoice.id"] == invoice_id for s in spans)
        assert all(s.attributes["step.latency_ms"] >= 0 for s in spans)
        agent = next(s for s in spans if s.name == "agent.classify")
        step = next(s for s in spans if s.name == "invoice.classify-discrepancy")
        assert agent.parent.span_id == step.context.span_id
        assert agent.attributes["agent.classification"] == case
        assert 0 <= agent.attributes["agent.confidence"] <= 1
        assert agent.attributes["gen_ai.usage.input_tokens"] == 0
        assert "input.total_amount" in agent.attributes
        assert step.attributes["agent.confidence"] == agent.attributes["agent.confidence"]


def test_failed_step_marks_span_error_and_trace_incomplete():
    @tracing.traced_step("invoice.update-finance")
    async def failing(invoice_id):
        raise RuntimeError("finance unavailable")

    invoice_id = str(uuid.uuid4())

    async def run():
        await extract(invoice_id)
        with pytest.raises(RuntimeError):
            await failing(invoice_id)

    asyncio.run(run())
    spans = spans_by_trace()[tracing.invoice_trace_id(invoice_id)]
    failed = next(s for s in spans if s.name == "invoice.update-finance")
    assert failed.status.status_code == trace.StatusCode.ERROR
    assert "agent.classify" in tracing.missing_spans(s.name for s in spans)


def test_jaeger_payload_parsing():
    payload = {"data": [{"spans": [{"operationName": name} for name in tracing.REQUIRED_SPANS]}]}
    assert tracing.missing_spans(tracing.jaeger_span_names(payload)) == []
    assert tracing.missing_spans(tracing.jaeger_span_names({"data": []})) == list(tracing.REQUIRED_SPANS)
