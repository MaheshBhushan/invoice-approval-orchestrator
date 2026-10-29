#!/usr/bin/env python3
"""Start the local stack and test the approval bridge with an ephemeral token."""
import os
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "APPROVAL_API_TOKEN": secrets.token_urlsafe(32), "POSTGRES_PORT": "0"}


def run(args):
    subprocess.run(args, cwd=ROOT, env=ENV, check=True)


print("1/4 Starting application services", flush=True)
run(["docker", "compose", "up", "-d", "--build", "postgres", "orchestration", "invoice-api", "finance-service", "invoice-workers"])
print("2/4 Applying approval migration and deploying BPMN", flush=True)
with (ROOT / "db/init/005_approval_responses.sql").open() as migration:
    subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "invoice_app", "-d", "invoice_workflow", "-v", "ON_ERROR_STOP=1"], stdin=migration, cwd=ROOT, env=ENV, check=True)
run(["docker", "compose", "cp", "bpmn/invoice-approval.bpmn", "invoice-api:/tmp/invoice-approval.bpmn"])
run(["docker", "compose", "cp", "forms/invoice-human-approval.form", "invoice-api:/tmp/invoice-human-approval.form"])
# Camunda's REST API can become ready after its container health probe succeeds.
for attempt in range(60):
    try:
        with urllib.request.urlopen("http://localhost:8000/health", timeout=2):
            pass
        run(["docker", "compose", "exec", "-T", "invoice-api", "python", "-c",
             "import httpx; r=httpx.post('http://orchestration:8080/v2/deployments', files=[('resources', ('invoice-approval.bpmn', open('/tmp/invoice-approval.bpmn', 'rb'), 'application/xml')), ('resources', ('invoice-human-approval.form', open('/tmp/invoice-human-approval.form', 'rb'), 'application/json'))]); r.raise_for_status()"])
        break
    # OSError covers URLError, timeouts and resets while uvicorn is still starting.
    except (OSError, subprocess.CalledProcessError):
        if attempt == 59:
            raise
        print(f"API readiness attempt {attempt + 1}/60", flush=True)
        time.sleep(2)
print("3/4 Testing approve, reject, duplicate responses and validation", flush=True)
run(["docker", "compose", "run", "--rm", "--no-deps", "-e", "APPROVAL_API_TOKEN",
     "-v", f"{ROOT / 'tests'}:/app/tests:ro", "-v", f"{ROOT / 'scripts'}:/app/scripts:ro", "invoice-api", "pytest", "-v", "/app/tests"])
print("4/4 Approval integration tests completed. Microsoft cloud execution requires tenant setup.", flush=True)
