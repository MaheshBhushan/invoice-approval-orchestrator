"""Create Azure DevOps Boards work items for invoices routed to human review.

Configuration comes from AZURE_DEVOPS_ORG_URL (e.g. https://dev.azure.com/contoso),
AZURE_DEVOPS_PROJECT, AZURE_DEVOPS_PAT, and optional AZURE_DEVOPS_WORK_ITEM_TYPE (default Task).
"""
import base64
import os
from urllib.parse import quote

import httpx

API_VERSION = "7.1"


def configured() -> bool:
    return all(os.getenv(k) for k in ("AZURE_DEVOPS_ORG_URL", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_PAT"))


def patch_document(invoice_id: str, payload: dict, decision: dict, baseline: dict | None = None) -> list[dict]:
    description = (
        f"<p>Invoice <b>{invoice_id}</b> from supplier {payload.get('supplier_code')} against "
        f"{payload.get('purchase_order_number')} needs human review.</p>"
        f"<p>Agent: {decision['classification']} (confidence {decision['confidence']:.2f})</p>"
        f"<p>{decision['explanation']}</p>"
        f"<ul>{''.join(f'<li>{e}</li>' for e in decision.get('evidence', []))}</ul>"
    )
    if baseline:
        description += f"<p>RF baseline: {baseline['classification']} (confidence {baseline['confidence']:.2f})</p>"
    return [
        {"op": "add", "path": "/fields/System.Title",
         "value": f"Review invoice {payload.get('supplier_invoice_number', invoice_id)}: {decision['classification']}"},
        {"op": "add", "path": "/fields/System.Description", "value": description},
        {"op": "add", "path": "/fields/System.Tags", "value": f"invoice-review; {decision['classification']}"},
    ]


def create_work_item(invoice_id: str, payload: dict, decision: dict, baseline: dict | None = None,
                     client: httpx.Client | None = None) -> dict:
    org_url = os.environ["AZURE_DEVOPS_ORG_URL"].rstrip("/")
    project = quote(os.environ["AZURE_DEVOPS_PROJECT"])
    work_item_type = quote(os.getenv("AZURE_DEVOPS_WORK_ITEM_TYPE", "Task"))
    token = base64.b64encode(f":{os.environ['AZURE_DEVOPS_PAT']}".encode()).decode()
    url = f"{org_url}/{project}/_apis/wit/workitems/${work_item_type}?api-version={API_VERSION}"
    http = client or httpx.Client(timeout=10)
    try:
        response = http.post(url, json=patch_document(invoice_id, payload, decision, baseline), headers={
            "Authorization": f"Basic {token}", "Content-Type": "application/json-patch+json"})
        response.raise_for_status()
        body = response.json()
        return {"id": body["id"], "url": body.get("_links", {}).get("html", {}).get("href", body.get("url"))}
    finally:
        if client is None:
            http.close()
