#!/usr/bin/env python3
"""Exercise recovery in the isolated Compose project; emit only observed results."""
import argparse
import json
import subprocess
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from recovery_demo import invoice_payload


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "invoice-recovery"


def compose(*args, capture=True):
    return subprocess.run(
        ["docker", "compose", "--project-name", PROJECT,
         "-f", "compose.yaml", "-f", "compose.recovery.yaml", *args],
        cwd=ROOT, check=True, text=True, capture_output=capture, timeout=15,
    ).stdout


def endpoint(service, port):
    return "http://" + compose("port", service, str(port)).strip()


def request(url, body=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        data = response.read()
        return json.loads(data) if data else {}


def wait_for(check, description, deadline):
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {description}")


def snapshot(invoice_ids):
    # Only UUIDs enter the SQL; the run never scans or modifies unrelated invoices.
    ids = ",".join(f"'{uuid.UUID(value)}'" for value in invoice_ids)
    statement = f"""
        SELECT json_build_object(
          'invoices', (SELECT json_agg(i) FROM invoices i WHERE invoice_id IN ({ids})),
          'metrics', (SELECT json_agg(m) FROM process_metrics m WHERE invoice_id IN ({ids})),
          'postings', (SELECT COALESCE(json_agg(p), '[]') FROM finance_postings p WHERE invoice_id IN ({ids})),
          'attempts', (SELECT COALESCE(json_agg(a ORDER BY step, attempt_number), '[]')
                       FROM processing_attempts a WHERE invoice_id IN ({ids})),
          'audit', (SELECT json_agg(e ORDER BY event_id) FROM audit_events e WHERE invoice_id IN ({ids}))
        )
    """
    return json.loads(compose(
        "exec", "-T", "postgres", "psql", "-U", "invoice_app", "-d", "invoice_workflow",
        "-v", "ON_ERROR_STOP=1", "-At", "-c", statement,
    ))


def verify(before, after, invoice_ids, scenario):
    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    previous = {event["event_id"]: event for event in before["audit"]}
    current = {event["event_id"]: event for event in after["audit"]}
    require(all(current.get(key) == value for key, value in previous.items()), "audit history changed")
    require({row["invoice_id"] for row in after["invoices"]} == set(invoice_ids), "invoice set mismatch")
    for invoice_id in invoice_ids:
        invoice = next(row for row in after["invoices"] if row["invoice_id"] == invoice_id)
        metrics = [row for row in after["metrics"] if row["invoice_id"] == invoice_id]
        postings = [row for row in after["postings"] if row["invoice_id"] == invoice_id]
        events = [row for row in after["audit"] if row["invoice_id"] == invoice_id]
        attempts = [row for row in after["attempts"] if row["invoice_id"] == invoice_id]
        require({row["step"] for row in attempts} == {
            "invoice.extract", "invoice.validate", "invoice.classify-discrepancy",
            "invoice.update-finance", "invoice.store-metrics",
        }, f"{invoice_id}: missing or unexpected processing steps")
        require(invoice["status"] == "POSTED", f"{invoice_id}: not posted")
        require(len(metrics) == 1 and metrics[0]["completed_at"] and metrics[0]["process_instance_key"],
                f"{invoice_id}: incomplete process metrics")
        require(len(postings) == 1, f"{invoice_id}: expected exactly one posting")
        require(postings[0]["idempotency_key"] == f"invoice:{invoice_id}", "posting key mismatch")
        required_events = ["INVOICE_RECEIVED", "VALIDATED", "AGENT_CLASSIFIED", "FINANCE_POSTED", "PROCESS_COMPLETED"]
        positions = []
        for name in [*required_events, "PROCESS_STARTED"]:
            matches = [row for row in events if row["event_type"] == name]
            require(len(matches) == 1, f"{invoice_id}: expected one {name}, got {len(matches)}")
            if name in required_events:
                positions.append(matches[0]["event_id"])
        require(positions == sorted(positions), "audit lifecycle order changed")
        posted = next(row for row in events if row["event_type"] == "FINANCE_POSTED")
        require(posted["details"]["external_reference"] == postings[0]["external_reference"],
                "finance audit reference mismatch")
        failures = [row for row in attempts if row["outcome"] == "RETRYABLE_FAILURE"]
        require(metrics[0]["retry_count"] == len(failures) == sum(
            row["event_type"] == "STEP_RETRYABLE_FAILURE" for row in events), "retry evidence mismatch")
        for step in {row["step"] for row in attempts}:
            group = [row for row in attempts if row["step"] == step]
            require([row["attempt_number"] for row in group] == list(range(1, len(group) + 1)),
                    f"{invoice_id}: noncontiguous attempts for {step}")
            require(any(row["outcome"] == "SUCCEEDED" for row in group), f"{step}: no successful attempt")
        for row in attempts:
            if row["outcome"] == "STARTED":
                require(scenario == "restart" and row["step"] == "invoice.update-finance"
                        and row["finished_at"] is None, "unexpected unfinished attempt")
            else:
                require(row["finished_at"] is not None, "finished attempt lacks timestamp")


def replay(api, proxy, invoice, created):
    intake = request(f"{api}/invoices", invoice)
    if not intake["reused"] or intake["invoice_id"] != created["invoice_id"] or str(
        intake["process_instance_key"]
    ) != str(created["process_instance_key"]):
        raise AssertionError("duplicate intake changed process identity")
    invoice_id = created["invoice_id"]
    req = urllib.request.Request(
        f"{proxy}/postings", data=json.dumps({"invoice_id": invoice_id, "amount": 1000, "currency": "EUR"}).encode(),
        headers={"Content-Type": "application/json", "Idempotency-Key": f"invoice:{invoice_id}"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return invoice_id, json.load(response)["external_reference"]


def run(scenario, count, concurrency, timeout):
    started = time.monotonic()
    deadline = started + timeout
    api = endpoint("invoice-api", 8000)
    proxy = endpoint("finance-fault-proxy", 8002)
    request(f"{api}/health")
    request(f"{proxy}/control", {"mode": {"restart": "hold", "delay": "delay", "outage": "drop"}[scenario]})
    invoices = [invoice_payload(f"load-{scenario}-{index}") for index in range(count)]
    worker_killed = False
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            created = list(pool.map(lambda value: request(f"{api}/invoices", value), invoices))
        invoice_ids = [value["invoice_id"] for value in created]
        print(f"{scenario}: submitted {len(created)} invoices", flush=True)
        expected_event = {"restart": "committed_hold", "delay": "committed_delay", "outage": "connection_dropped"}[scenario]
        events = wait_for(
            lambda: observed_faults(proxy, expected_event, 1 if scenario == "restart" else count),
            expected_event, deadline,
        )
        if scenario == "restart":
            # The proxy has committed a posting but has withheld its response.
            # Kill immediately, before snapshots or duplicate HTTP work can release the job.
            compose("kill", "--signal", "SIGKILL", "invoice-workers")
            worker_killed = True
            request(f"{proxy}/release", {})
        before = snapshot(invoice_ids)
        print(f"{scenario}: observed {expected_event}; replaying concurrent deliveries", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(replay, api, proxy, invoice, result)
                       for invoice, result in zip(invoices, created) for _ in range(3)]
            references = [future.result() for future in futures]
        if worker_killed:
            compose("start", "invoice-workers")
            worker_killed = False

        def completed():
            state = snapshot(invoice_ids)
            return state if all(row["completed_at"] for row in state["metrics"]) else None

        after = wait_for(completed, "completed workflows", deadline)
        verify(before, after, invoice_ids, scenario)
        for invoice_id, reference in references:
            if not any(row["invoice_id"] == invoice_id and row["external_reference"] == reference
                       for row in after["postings"]):
                raise AssertionError("duplicate callback changed finance reference")
        events = request(f"{proxy}/events")
        affected = {row["invoice_id"] for row in events if row["event"] == expected_event}
        if scenario != "restart" and affected != set(invoice_ids):
            raise AssertionError("fault was not exercised for every invoice")
        if scenario != "restart" and not all(
            any(row["invoice_id"] == invoice_id and row["outcome"] == "RETRYABLE_FAILURE"
                for row in after["attempts"]) for invoice_id in invoice_ids
        ):
            raise AssertionError("fault did not cause a durable retry for every invoice")
        if scenario == "restart" and not any(row["outcome"] == "STARTED" for row in after["attempts"]):
            raise AssertionError("worker restart missed the in-flight posting barrier")
        result = {"scenario": scenario, "invoice_ids": invoice_ids, "postings": len(after["postings"]),
                  "duplicate_callbacks": len(references), "fault_events": events,
                  "retry_count": sum(row["retry_count"] for row in after["metrics"]),
                  "elapsed_seconds": round(time.monotonic() - started, 3), "verified": True}
        print(json.dumps(result), flush=True)
        return result
    finally:
        # Release held requests and restore workers even after a failed assertion.
        try:
            request(f"{proxy}/release", {})
        finally:
            if worker_killed:
                compose("start", "invoice-workers")


def observed_faults(proxy, expected_event, count):
    events = [event for event in request(f"{proxy}/events") if event["event"] == expected_event]
    return events if len(events) >= count else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["restart", "delay", "outage"], required=True)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", type=Path, help="Write observed evidence as JSON")
    args = parser.parse_args()
    if args.count < 1 or args.concurrency < 2 or args.timeout <= 0:
        parser.error("count must be positive, concurrency at least two, and timeout positive")
    result = run(args.scenario, args.count, args.concurrency, args.timeout)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
