from common.classifier import GRAPH


def decision(total, po_total, notes=None):
    return GRAPH.invoke(
        {
            "invoice": {
                "total_amount": total,
                "currency": "EUR",
                "line_notes": notes or [],
            },
            "purchase_order": {"total_amount": po_total},
            "validation": {},
        }
    )["decision"]


def test_classifier_ground_truth_cases():
    assert decision(1000, 1000)["classification"] == "no_discrepancy"
    assert decision(1190, 1000)["classification"] == "amount_mismatch"
    ambiguous = decision(2515, 2500, ["Service surcharge per verbal agreement"])
    assert ambiguous["classification"] == "ambiguous_discrepancy"
    assert ambiguous["confidence"] < 0.8
