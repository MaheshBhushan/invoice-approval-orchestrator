"""Local-only finance fault proxy. Never include this service in a deployment."""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Faults:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "pass"
        self.seen = set()
        self.events = []
        self.release = threading.Event()

    def configure(self, mode):
        if mode not in {"pass", "drop", "delay", "hold"}:
            raise ValueError("unknown fault mode")
        with self.lock:
            self.mode = mode
            self.seen.clear()
            self.events.clear()
            self.release.clear()

    def claim(self, invoice_id):
        with self.lock:
            mode = "pass" if invoice_id in self.seen else self.mode
            self.seen.add(invoice_id)
            return mode

    def record(self, invoice_id, event):
        with self.lock:
            self.events.append({"invoice_id": invoice_id, "event": event})

    def snapshot(self):
        with self.lock:
            return list(self.events)


FAULTS = Faults()


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the worker times out or gets killed.

    def do_GET(self):
        if self.path == "/events":
            self.respond(200, FAULTS.snapshot())
        else:
            self.respond(404, {})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/control":
                FAULTS.configure(body["mode"])
                self.respond(200, {"mode": body["mode"]})
                return
            if self.path == "/release":
                FAULTS.release.set()
                self.respond(200, {})
                return
            if self.path != "/postings":
                self.respond(404, {})
                return
            invoice_id = body["invoice_id"]
            mode = FAULTS.claim(invoice_id)
            if mode == "drop":
                FAULTS.record(invoice_id, "connection_dropped")
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            req = urllib.request.Request(
                "http://finance-service:8001/postings", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Idempotency-Key": self.headers["Idempotency-Key"]},
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.load(response)
            if mode in {"delay", "hold"}:
                FAULTS.record(invoice_id, "committed_" + mode)
                if mode == "delay":
                    time.sleep(1.0)  # Exceeds the recovery worker's 0.5 second timeout.
                elif not FAULTS.release.wait(15):
                    self.respond(504, {"error": "restart barrier timed out"})
                    return
            self.respond(201, result)
        except urllib.error.HTTPError as exc:
            self.respond(exc.code, {"error": "finance request rejected"})
        except (ValueError, KeyError) as exc:
            self.respond(400, {"error": str(exc)})
        except (OSError, urllib.error.URLError) as exc:
            self.respond(502, {"error": str(exc)})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8002), Handler).serve_forever()
