from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from common.tracing import record_agent_decision, step_span


class AgentState(TypedDict, total=False):
    invoice: dict
    purchase_order: dict
    validation: dict
    decision: dict


def classify(state: AgentState) -> AgentState:
    invoice = state["invoice"]
    with step_span("agent.classify", invoice.get("invoice_id", "unknown"),
                   **{"input.total_amount": invoice.get("total_amount"),
                      "input.purchase_order_total": state["purchase_order"].get("total_amount"),
                      "input.line_notes": invoice.get("line_notes", [])}) as span:
        result = _classify(state)
        record_agent_decision(span, result["decision"])
        return result


def _classify(state: AgentState) -> AgentState:
    """Deterministic demo classifier; an LLM adapter can replace this node without changing routing."""
    invoice = state["invoice"]
    po = state["purchase_order"]
    extraction = invoice.get("ocr_extraction") or {}
    if extraction.get("requires_human_review"):
        return {"decision": {
            "classification": "low_confidence_extraction",
            "severity": "medium",
            "confidence": float(extraction.get("confidence", 0.0)),
            "recommended_action": "human_review",
            "evidence": [f"ocr.confidence={extraction.get('ocr_confidence')}",
                         f"extraction.confidence={extraction.get('extraction_confidence')}", *extraction.get("issues", [])],
            "explanation": "Fields read from the scanned invoice are not reliable enough to post without review.",
        }}
    notes = " ".join(invoice.get("line_notes", [])).lower()
    total = float(invoice["total_amount"])
    po_total = float(po["total_amount"])
    difference = round(total - po_total, 2)

    if "verbal agreement" in notes or (difference and abs(difference) / po_total <= 0.02):
        decision = {
            "classification": "ambiguous_discrepancy",
            "severity": "medium",
            "confidence": 0.55,
            "recommended_action": "human_review",
            "evidence": [f"invoice.total={total:.2f}", f"purchase_order.total={po_total:.2f}", *invoice.get("line_notes", [])],
            "explanation": "The small variance cannot be resolved from structured purchase-order data.",
        }
    elif difference:
        decision = {
            "classification": "amount_mismatch",
            "severity": "medium" if abs(difference) / po_total < 0.25 else "high",
            "confidence": 0.96,
            "recommended_action": "post_with_exception",
            "evidence": [f"invoice.total={total:.2f}", f"purchase_order.total={po_total:.2f}", f"difference={difference:.2f}"],
            "explanation": f"The invoice differs from the purchase order by {difference:.2f} {invoice['currency']}.",
        }
    else:
        decision = {
            "classification": "no_discrepancy",
            "severity": "none",
            "confidence": 0.99,
            "recommended_action": "auto_post",
            "evidence": [f"invoice.total={total:.2f}", f"purchase_order.total={po_total:.2f}"],
            "explanation": "Supplier, currency, and total match the open purchase order.",
        }
    return {"decision": decision}


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("classify", classify)
    graph.add_edge(START, "classify")
    graph.add_edge("classify", END)
    return graph.compile()


GRAPH = build_graph()
