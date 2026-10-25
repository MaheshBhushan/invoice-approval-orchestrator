"""Random Forest discrepancy-classification baseline that runs beside the LangGraph agent.

The model trains on a seeded synthetic set labelled by the deterministic classifier rules, so
agreement with the agent measures how well tabular features recover those rules. It is a
baseline for comparison and never overrides the agent decision.
"""
import random
from functools import lru_cache

from sklearn.ensemble import RandomForestClassifier

FEATURES = ["abs_variance_ratio", "signed_variance_ratio", "has_verbal_agreement", "has_any_note", "ocr_needs_review"]
CLASSES = ["no_discrepancy", "amount_mismatch", "ambiguous_discrepancy", "low_confidence_extraction"]


def features(invoice: dict, purchase_order: dict) -> list[float]:
    total = float(invoice["total_amount"])
    po_total = float(purchase_order["total_amount"])
    ratio = (total - po_total) / po_total if po_total else 0.0
    notes = " ".join(invoice.get("line_notes", [])).lower()
    extraction = invoice.get("ocr_extraction") or {}
    return [abs(ratio), ratio, float("verbal agreement" in notes), float(bool(notes)),
            float(bool(extraction.get("requires_human_review")))]


def rule_label(row: list[float]) -> str:
    abs_ratio, _, verbal, _, ocr_review = row
    if ocr_review:
        return "low_confidence_extraction"
    if verbal or (abs_ratio and abs_ratio <= 0.02):
        return "ambiguous_discrepancy"
    return "amount_mismatch" if abs_ratio else "no_discrepancy"


def synthetic_training_set(n: int = 2000, seed: int = 7):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        kind = rng.random()
        ratio = 0.0 if kind < 0.3 else rng.uniform(-0.02, 0.02) if kind < 0.5 else rng.choice([-1, 1]) * rng.uniform(0.02, 0.6)
        has_note = rng.random() < 0.3
        verbal = has_note and rng.random() < 0.4
        rows.append([abs(ratio), ratio, float(verbal), float(has_note), float(rng.random() < 0.08)])
    return rows, [rule_label(r) for r in rows]


@lru_cache(maxsize=1)
def model() -> RandomForestClassifier:
    x, y = synthetic_training_set()
    return RandomForestClassifier(n_estimators=100, max_depth=8, random_state=7).fit(x, y)


def predict(invoice: dict, purchase_order: dict) -> dict:
    clf = model()
    probabilities = clf.predict_proba([features(invoice, purchase_order)])[0]
    best = int(probabilities.argmax())
    return {
        "classification": str(clf.classes_[best]),
        "confidence": round(float(probabilities[best]), 4),
        "probabilities": {str(c): round(float(p), 4) for c, p in zip(clf.classes_, probabilities)},
        "model": "sklearn.RandomForestClassifier",
    }


def compare(agent_decision: dict, baseline: dict) -> dict:
    return {**baseline, "agrees_with_agent": baseline["classification"] == agent_decision.get("classification")}
