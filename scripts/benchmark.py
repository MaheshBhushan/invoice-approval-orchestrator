#!/usr/bin/env python3
import argparse
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_URL = "http://localhost:8000"
CAMUNDA_URL = "http://localhost:8088"


def request(method, url, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response) if response.length != 0 else {}


def query_json(statement):
    result = subprocess.run(
        [
            "docker",
            "compose",
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
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def make_invoice(run_id, index, case):
    values = {
        "idempotency_key": f"bench-{run_id}-{index:03d}",
        "supplier_code": "SUP-001",
        "purchase_order_number": "PO-1001",
        "supplier_invoice_number": f"BENCH-{run_id}-{index:03d}",
        "invoice_date": "2026-08-21",
        "currency": "EUR",
        "net_amount": 840.34,
        "tax_amount": 159.66,
        "total_amount": 1000.00,
    }
    if case == "amount_mismatch":
        values.update(net_amount=1000.00, tax_amount=190.00, total_amount=1190.00)
    elif case == "ambiguous_discrepancy":
        values.update(
            supplier_code="SUP-002",
            purchase_order_number="PO-2001",
            net_amount=2113.45,
            tax_amount=401.55,
            total_amount=2515.00,
            line_notes=["Service surcharge per verbal agreement"],
        )
    return values


def complete_ready_tasks(process_keys):
    response = request(
        "POST",
        f"{CAMUNDA_URL}/v2/user-tasks/search",
        {
            "filter": {"state": "CREATED", "processDefinitionId": "invoice-approval"},
            "page": {"limit": 1000},
        },
    )
    completed = 0
    for task in response["items"]:
        if task["processInstanceKey"] in process_keys:
            try:
                request(
                    "POST",
                    f"{CAMUNDA_URL}/v2/user-tasks/{task['userTaskKey']}/completion",
                    {"variables": {"human_decision": "approved", "reviewer_id": "benchmark"}},
                )
                completed += 1
            except urllib.error.HTTPError as exc:
                if exc.code not in {404, 409}:
                    raise
    return completed


def database_rows(run_id):
    return query_json(
        "SELECT COALESCE(json_agg(row_to_json(result)), '[]'::json) FROM ("
        "SELECT i.invoice_id::text, i.idempotency_key, i.status, i.agent_classification, "
        "m.straight_through, m.human_review_count, m.retry_count, "
        "round((extract(epoch FROM (m.completed_at - m.started_at)) * 1000)::numeric, 2) AS latency_ms "
        "FROM invoices i JOIN process_metrics m USING (invoice_id) "
        f"WHERE i.idempotency_key LIKE 'bench-{run_id}-%' ORDER BY i.idempotency_key) result"
    )


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def write_results(result):
    output = ROOT / "benchmark"
    output.mkdir(exist_ok=True)
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    metrics = result["metrics"]
    markdown = f"""# Invoice workflow benchmark

Run `{result['run_id']}` processed {result['invoice_count']} invoices on {result['generated_at']}.

| Metric | Result |
|---|---:|
| Completed | {metrics['completed']} / {result['invoice_count']} |
| Mean completion latency | {metrics['latency_ms']['mean']:.2f} ms |
| p50 completion latency | {metrics['latency_ms']['p50']:.2f} ms |
| p95 completion latency | {metrics['latency_ms']['p95']:.2f} ms |
| Straight-through-processing rate | {metrics['straight_through_rate']:.1%} |
| Human-review rate | {metrics['human_review_rate']:.1%} |
| Duplicate deliveries prevented | {metrics['duplicates_prevented']} |
| Durable retries recorded | {metrics['retries']} |
| Classification accuracy | {metrics['classification_accuracy']:.1%} |

Ground truth is deterministic seeded data: exact PO match, 19% amount mismatch, and ambiguous surcharge.
"""
    (output / "results.md").write_text(markdown)


def main():
    parser = argparse.ArgumentParser(description="Benchmark the executable invoice workflow")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.count < 100:
        parser.error("--count must be at least 100 for a verifiable benchmark")

    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait"], cwd=ROOT, check=True
    )
    run_id = uuid.uuid4().hex[:8]
    expected = {}
    process_keys = set()
    submitted = []
    started = time.monotonic()
    for index in range(args.count):
        bucket = index % 10
        case = "no_discrepancy" if bucket < 7 else "amount_mismatch" if bucket < 9 else "ambiguous_discrepancy"
        invoice = make_invoice(run_id, index, case)
        response = request("POST", f"{API_URL}/invoices", invoice)
        expected[response["invoice_id"]] = case
        process_keys.add(str(response["process_instance_key"]))
        submitted.append(invoice)

    deadline = time.monotonic() + args.timeout
    rows = []
    while time.monotonic() < deadline:
        complete_ready_tasks(process_keys)
        rows = database_rows(run_id)
        if len(rows) == args.count and all(
            row["status"] == "POSTED" and row["latency_ms"] is not None for row in rows
        ):
            break
        time.sleep(0.5)
    else:
        states = {row["status"] for row in rows}
        raise RuntimeError(f"benchmark timed out with {len(rows)}/{args.count} rows in {states}")

    duplicates_prevented = 0
    for invoice in submitted[::10]:
        if request("POST", f"{API_URL}/invoices", invoice)["reused"]:
            duplicates_prevented += 1

    latencies = [float(row["latency_ms"]) for row in rows]
    correct = sum(
        row["agent_classification"] == expected[row["invoice_id"]] for row in rows
    )
    result = {
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "invoice_count": args.count,
        "wall_clock_seconds": round(time.monotonic() - started, 3),
        "case_distribution": {
            case: list(expected.values()).count(case) for case in sorted(set(expected.values()))
        },
        "metrics": {
            "completed": len(rows),
            "latency_ms": {
                "mean": round(statistics.mean(latencies), 2),
                "p50": round(statistics.median(latencies), 2),
                "p95": round(percentile(latencies, 0.95), 2),
            },
            "straight_through_rate": sum(row["straight_through"] for row in rows) / args.count,
            "human_review_rate": sum(row["human_review_count"] > 0 for row in rows) / args.count,
            "duplicates_prevented": duplicates_prevented,
            "retries": sum(row["retry_count"] for row in rows),
            "classification_accuracy": correct / args.count,
        },
    }
    write_results(result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
