"""Fast checks for deterministic fault selection and audit verification."""
import copy
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from recovery_load import snapshot, verify
from recovery_proxy import Faults


def test_fault_is_claimed_once_under_concurrent_callbacks():
    faults = Faults()
    faults.configure("drop")
    with ThreadPoolExecutor(max_workers=8) as pool:
        modes = list(pool.map(lambda _: faults.claim("invoice-1"), range(32)))
    assert modes.count("drop") == 1
    assert modes.count("pass") == 31
    assert faults.claim("invoice-2") == "drop"
    faults.record("invoice-1", "connection_dropped")
    saved = faults.snapshot()
    faults.configure("delay")
    assert faults.claim("invoice-1") == "delay"
    assert saved == [{"invoice_id": "invoice-1", "event": "connection_dropped"}]
    assert faults.snapshot() == []
    with pytest.raises(ValueError, match="unknown fault mode"):
        faults.configure("typo")


def valid_snapshot():
    names = ["INVOICE_RECEIVED", "PROCESS_STARTED", "VALIDATED", "AGENT_CLASSIFIED",
             "FINANCE_POSTED", "PROCESS_COMPLETED"]
    return {
        "invoices": [{"invoice_id": "i", "status": "POSTED"}],
        "metrics": [{"invoice_id": "i", "completed_at": "now", "process_instance_key": "123", "retry_count": 0}],
        "postings": [{"invoice_id": "i", "idempotency_key": "invoice:i", "external_reference": "FIN-1"}],
        "attempts": [{"invoice_id": "i", "step": step, "attempt_number": 1,
                      "outcome": "SUCCEEDED", "finished_at": "now"} for step in (
                          "invoice.classify-discrepancy", "invoice.extract", "invoice.store-metrics",
                          "invoice.update-finance", "invoice.validate")],
        "audit": [{"invoice_id": "i", "event_id": index, "event_type": name,
                   "details": {"external_reference": "FIN-1"} if name == "FINANCE_POSTED" else {}}
                  for index, name in enumerate(names)],
    }


def test_verifier_accepts_recovery_and_rejects_corrupted_evidence():
    original = valid_snapshot()
    before = copy.deepcopy(original)
    before["audit"] = before["audit"][:2]
    verify(before, original, ["i"], "delay")
    corruptions = [
        lambda state: state["postings"].append(copy.deepcopy(state["postings"][0])),
        lambda state: state["audit"][0].update(details={"changed": True}),
        lambda state: state["audit"].pop(),
        lambda state: state["audit"][4]["details"].update(external_reference="WRONG"),
        lambda state: state["metrics"][0].update(retry_count=1),
        lambda state: state["attempts"][0].update(attempt_number=2),
        lambda state: state["attempts"][0].update(finished_at=None),
        lambda state: state["attempts"].pop(),
    ]
    for corrupt in corruptions:
        state = copy.deepcopy(original)
        corrupt(state)
        with pytest.raises(AssertionError):
            verify(before, state, ["i"], "delay")


def test_snapshot_rejects_non_uuid_before_running_sql():
    with pytest.raises(ValueError):
        snapshot(["x'; SELECT 1; --"])
