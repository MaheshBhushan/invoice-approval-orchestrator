import json

import httpx

from common import azure_devops, rf_baseline
from common.classifier import GRAPH


def case(total, po_total, notes=None):
    return {"total_amount": total, "currency": "EUR", "line_notes": notes or []}, {"total_amount": po_total}


def test_baseline_matches_agent_on_ground_truth_cases():
    for invoice, po in [case(1000, 1000), case(1190, 1000), case(2515, 2500, ["Service surcharge per verbal agreement"])]:
        agent = GRAPH.invoke({"invoice": invoice, "purchase_order": po, "validation": {}})["decision"]
        result = rf_baseline.compare(agent, rf_baseline.predict(invoice, po))
        assert result["agrees_with_agent"], result
        assert 0 <= result["confidence"] <= 1


def test_baseline_holdout_accuracy_against_rule_labels():
    x, y = rf_baseline.synthetic_training_set(n=500, seed=99)
    accuracy = rf_baseline.model().score(x, y)
    assert accuracy >= 0.95


def test_work_item_request_uses_json_patch(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_ORG_URL", "https://dev.azure.com/contoso/")
    monkeypatch.setenv("AZURE_DEVOPS_PROJECT", "Finance Ops")
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"id": 42, "_links": {"html": {"href": "https://dev.azure.com/contoso/_workitems/edit/42"}}})

    invoice, po = case(2515, 2500, ["Service surcharge per verbal agreement"])
    decision = GRAPH.invoke({"invoice": invoice, "purchase_order": po, "validation": {}})["decision"]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = azure_devops.create_work_item("inv-1", invoice, decision, rf_baseline.predict(invoice, po), client=client)

    request = seen["request"]
    assert result == {"id": 42, "url": "https://dev.azure.com/contoso/_workitems/edit/42"}
    assert str(request.url) == "https://dev.azure.com/contoso/Finance%20Ops/_apis/wit/workitems/$Task?api-version=7.1"
    assert request.headers["content-type"] == "application/json-patch+json"
    assert request.headers["authorization"].startswith("Basic ")
    ops = json.loads(request.content)
    assert {op["path"] for op in ops} == {"/fields/System.Title", "/fields/System.Description", "/fields/System.Tags"}
    assert "ambiguous_discrepancy" in ops[0]["value"]


def test_unconfigured_azure_devops_is_skipped(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    assert not azure_devops.configured()
