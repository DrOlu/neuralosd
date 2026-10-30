"""neuralosd_sdk client — transport, error model, auth, retries.

Runs the real client against a stub HTTP server (threaded) so retries,
refusals and auth behavior are exercised over actual sockets, not mocks.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from neuralosd_sdk import (
    AuthError,
    NeuralOSDClient,
    RefusalError,
    TransportError,
)

TOKEN = "sdk-test-token"
ENVELOPE = {"ask_id": "x", "probe": "row_count", "results": [{"count": 4}],
            "mode": "deterministic", "latency_ms": 12}


class Stub(BaseHTTPRequestHandler):
    """Configurable stub: fail next N /ask with 500, count auth checks."""
    behavior = {"fail_500_left": 0, "got_auth": [], "lock": threading.Lock()}

    def _reply(self, code, payload):
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authorized(self):
        auth = self.headers.get("Authorization") or ""
        alt = self.headers.get("X-API-Token") or ""
        with self.behavior["lock"]:
            self.behavior["got_auth"].append(auth or alt)
        return auth == f"Bearer {TOKEN}" or alt == TOKEN

    def _send(self, code, payload):
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._authorized() and self.path not in ("/healthz", "/ready"):
            self._reply(401, {"error": "unauthorized"})
            return
        self._reply(200, {"ok": True, "instance": "stub"})

    def do_POST(self):
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with self.behavior["lock"]:
            if self.behavior["fail_500_left"] > 0:
                self.behavior["fail_500_left"] -= 1
                self._reply(500, {"error": "transient"})
                return
        if body.get("question") == "refuse me":
            self._reply(422, {"error": "no_probe_matches",
                              "refusal_reason": "no_probe_matches"})
        else:
            self._reply(200, ENVELOPE)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def server():
    Stub.behavior.update({"fail_500_left": 0, "got_auth": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ask_returns_typed_result(server):
    c = NeuralOSDClient(server, token=TOKEN, backoff=0.01)
    r = c.ask("how many rows")
    assert r.probe == "row_count"
    assert r.results == [{"count": 4}]
    assert r.mode == "deterministic"
    assert r.raw["ask_id"] == "x"


def test_retry_on_5xx_then_success(server):
    with Stub.behavior["lock"]:
        Stub.behavior["fail_500_left"] = 2
    c = NeuralOSDClient(server, token=TOKEN, backoff=0.01, max_retries=3)
    r = c.ask("how many rows")           # retried through two 500s
    assert r.probe == "row_count"


def test_no_retry_on_4xx(server):
    with Stub.behavior["lock"]:
        Stub.behavior["fail_500_left"] = 0
    c = NeuralOSDClient(server, token=TOKEN, backoff=0.01, max_retries=3)
    with pytest.raises(RefusalError) as e:
        c.ask("refuse me")
    assert e.value.envelope["refusal_reason"] == "no_probe_matches"


def test_wrong_token_raises_auth_error(server):
    c = NeuralOSDClient(server, token="wrong")
    with pytest.raises(AuthError):
        c.ask("anything")


def test_missing_token_raises_auth_error(server):
    c = NeuralOSDClient(server)          # server requires a token
    with pytest.raises(AuthError):
        c.ask("anything")


def test_health_is_open_without_token(server):
    c = NeuralOSDClient(server)          # liveness needs no credentials
    assert c.health() == {"ok": True, "instance": "stub"}


def test_connection_failure_raises_transport_error():
    c = NeuralOSDClient("http://127.0.0.1:1", max_retries=1, backoff=0.01,
                        timeout=2)
    with pytest.raises(TransportError):
        c.ask("anything", )
