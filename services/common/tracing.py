"""OpenTelemetry spans for agent and worker steps, grouped into one trace per invoice.

Zeebe does not carry trace context between jobs, so every span for an invoice joins a
trace whose ID is derived from the invoice ID. Jaeger can then fetch the full trace with
``GET /api/traces/<invoice_trace_id(invoice_id)>``.
"""
import functools
import json
import os
import time
import uuid
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

TRACE_NAMESPACE = uuid.UUID("6f3b1c52-9a0e-4c55-8d0b-2f7e3c9a1d40")
# Spans a POSTED invoice must have for its trace to count as complete.
REQUIRED_SPANS = (
    "invoice.extract",
    "invoice.validate",
    "invoice.classify-discrepancy",
    "agent.classify",
    "invoice.update-finance",
    "invoice.store-metrics",
)
_MAX_ATTRIBUTE_CHARS = 2000


def configure(service_name: str) -> bool:
    """Install an OTLP/HTTP exporter when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return True


def tracer():
    return trace.get_tracer("invoice-approval-orchestrator")


def invoice_trace_id(invoice_id: str) -> str:
    return uuid.uuid5(TRACE_NAMESPACE, str(invoice_id)).hex


def invoice_context(invoice_id: str):
    parent = SpanContext(
        trace_id=int(invoice_trace_id(invoice_id), 16),
        span_id=int(invoice_trace_id(f"{invoice_id}:root")[:16], 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(parent))


def _attribute(value):
    if isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, default=str, sort_keys=True)[:_MAX_ATTRIBUTE_CHARS]


@contextmanager
def step_span(name: str, invoice_id: str, **attributes):
    """Open a span for one step; the active span stays the parent inside an existing trace."""
    current = trace.get_current_span().get_span_context()
    ctx = None if current.is_valid else invoice_context(invoice_id)
    started = time.perf_counter()
    with tracer().start_as_current_span(name, context=ctx, record_exception=True) as span:
        span.set_attribute("invoice.id", str(invoice_id))
        span.set_attribute("agent.step", name)
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, _attribute(value))
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            span.set_attribute("step.latency_ms", round((time.perf_counter() - started) * 1000, 3))


def traced_step(name: str):
    """Decorate an async Zeebe task whose first argument is invoice_id."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(invoice_id: str, *args, **kwargs):
            with step_span(name, invoice_id, **{f"input.{k}": v for k, v in kwargs.items()}) as span:
                result = await func(invoice_id, *args, **kwargs)
                if isinstance(result, dict):
                    for key in ("agent_confidence", "agent_requires_human"):
                        if key in result:
                            span.set_attribute(key.replace("_", ".", 1), result[key])
                return result
        return wrapper
    return decorator


def record_agent_decision(span, decision: dict):
    usage = decision.get("usage") or {}
    span.set_attribute("agent.classification", decision["classification"])
    span.set_attribute("agent.confidence", float(decision["confidence"]))
    span.set_attribute("agent.recommended_action", decision["recommended_action"])
    span.set_attribute("agent.model", decision.get("model", "deterministic-rules"))
    span.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input_tokens", 0)))
    span.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output_tokens", 0)))


def missing_spans(span_names) -> list[str]:
    names = set(span_names)
    return [name for name in REQUIRED_SPANS if name not in names]


def jaeger_span_names(payload: dict) -> list[str]:
    """Extract operation names from a Jaeger ``/api/traces/<id>`` response."""
    return [span["operationName"] for item in payload.get("data", []) for span in item.get("spans", [])]

