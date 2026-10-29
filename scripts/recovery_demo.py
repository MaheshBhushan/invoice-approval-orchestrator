#!/usr/bin/env python3
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_URL = "http://localhost:8000"
CAMUNDA_URL = "http://localhost:8088"


def compose(*args, capture=False):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )


def request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response) if response.length != 0 else {}


def sql(statement):
    result = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "invoice_app",
        "-d",
        "invoice_workflow",
        "-At",
        "-c",
        statement,
        capture=True,
    )
    return result.stdout.strip()


def invoice_payload(label):
    suffix = uuid.uuid4().hex[:10]
    return {
        "idempotency_key": f"recovery-{label}-{suffix}",
        "supplier_code": "SUP-001",
        "purchase_order_number": "PO-1001",
        "supplier_invoice_number": f"RECOVERY-{label.upper()}-{suffix}",
        "invoice_date": "2026-08-21",
        "currency": "EUR",
        "net_amount": 840.34,
        "tax_amount": 159.66,
        "total_amount": 1000.00,
    }


def submit(label):
    return request("POST", f"{API_URL}/invoices", invoice_payload(label))


def wait_until(check, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {description}")


def status(invoice_id):
    return sql(f"SELECT status FROM invoices WHERE invoice_id = '{invoice_id}'")


def retry_count(invoice_id):
    return int(sql(f"SELECT retry_count FROM process_metrics WHERE invoice_id = '{invoice_id}'") or 0)


def posting_count(invoice_id):
    return int(sql(f"SELECT count(*) FROM finance_postings WHERE invoice_id = '{invoice_id}'"))


def incidents(process_key):
    try:
        return request(
            "POST",
            f"{CAMUNDA_URL}/v2/process-instances/{process_key}/incidents/search",
            {"filter": {"state": "ACTIVE"}, "page": {"limit": 10}},
        )["items"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def deploy(path):
    subprocess.run(
        [
            "curl",
            "-fsS",
            "-X",
            "POST",
            f"{CAMUNDA_URL}/v2/deployments",
            "-F",
            f"resources=@{path}",
            "-F",
            f"resources=@{ROOT / 'forms/invoice-human-approval.form'}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main():
    compose("up", "-d", "--wait")
    finance_running = True
    original_bpmn = ROOT / "bpmn/invoice-approval.bpmn"
    try:
        compose("stop", "finance-service")
        finance_running = False
        transient = submit("transient")
        wait_until(
            lambda: retry_count(transient["invoice_id"]) >= 1,
            "a recorded retryable finance failure",
            timeout=30,
        )
        compose("start", "--wait", "finance-service")
        finance_running = True
        wait_until(
            lambda: status(transient["invoice_id"]) == "POSTED",
            "successful posting after finance restoration",
            timeout=90,
        )
        assert posting_count(transient["invoice_id"]) == 1
        print(
            f"PASS transient recovery: invoice={transient['invoice_id']} "
            f"retries={retry_count(transient['invoice_id'])} postings=1"
        )

        accelerated = original_bpmn.read_text().replace(
            '<zeebe:taskDefinition type="invoice.update-finance" retries="5" />',
            '<zeebe:taskDefinition type="invoice.update-finance" retries="1" />',
        )
        if accelerated == original_bpmn.read_text():
            raise RuntimeError("could not create the accelerated incident-test BPMN")
        with tempfile.NamedTemporaryFile("w", suffix=".bpmn") as handle:
            handle.write(accelerated)
            handle.flush()
            deploy(handle.name)

        compose("stop", "finance-service")
        finance_running = False
        terminal = submit("terminal")
        incident = wait_until(
            lambda: (items := incidents(terminal["process_instance_key"])) and items[0],
            "Camunda incident after exhausted retries",
            timeout=35,
        )
        print(
            f"PASS terminal failure: invoice={terminal['invoice_id']} "
            f"incident={incident['incidentKey']} type={incident['errorType']}"
        )

        compose("start", "--wait", "finance-service")
        finance_running = True
        request(
            "PATCH",
            f"{CAMUNDA_URL}/v2/jobs/{incident['jobKey']}",
            {"changeset": {"retries": 1}},
        )
        request("POST", f"{CAMUNDA_URL}/v2/incidents/{incident['incidentKey']}/resolution", {})
        wait_until(
            lambda: status(terminal["invoice_id"]) == "POSTED",
            "incident resolution after finance restoration",
            timeout=45,
        )
        assert posting_count(terminal["invoice_id"]) == 1
        print(
            f"PASS incident recovery: invoice={terminal['invoice_id']} "
            f"retries={retry_count(terminal['invoice_id'])} postings=1"
        )
    finally:
        if not finance_running:
            compose("start", "--wait", "finance-service")
        deploy(original_bpmn)


if __name__ == "__main__":
    main()
