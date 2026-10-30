"""`neuralosd serve --auth-token` — real-server auth E2E.

Spins the actual service (with a real instance) and asserts:
  - /ask without a token        -> 401
  - /ask with the right token   -> 200 envelope
  - /healthz stays open         -> liveness must not need credentials
  - the SDK client works against the authed server
"""

import json
import threading

from neuralosd.instance import Instance
from neuralosd.service import serve
from neuralosd_sdk import NeuralOSDClient

TOKEN = "e2e-token"

def test_serve_auth_real():
    """End-to-end through neuralosd.service.serve with token auth on."""
    import socket
    import time
    import urllib.error
    import urllib.request

    from neuralosd.probe import probe

    @probe(description="Count rows", triggers=["how many rows"])
    def row_count():
        return {"count": 4}

    inst = Instance(name="auth-e2e", probes=[row_count])

    # pick a free port ourselves
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    t = threading.Thread(
        target=serve, args=(inst,), kwargs={"port": port, "host": "127.0.0.1",
                                            "auth_token": TOKEN}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    def req(path, method="GET", body=None, token=TOKEN):
        r = urllib.request.Request(f"{base}{path}", method=method,
                                   data=json.dumps(body).encode() if body else None)
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            resp = urllib.request.urlopen(r, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    code, _ = req("/ask", method="POST", body={"question": "how many rows"},
                  token=None)
    assert code == 401                       # no token → refused
    code, _ = req("/ask", method="POST", body={"question": "how many rows"},
                  token="wrong")
    assert code == 401                       # wrong token → refused
    code, body = req("/ask", method="POST",
                     body={"question": "how many rows"})
    assert code == 200 and body["results"][0]["count"] == 4
    code, _ = req("/healthz", token=None)    # liveness stays open
    assert code == 200

    # and through the SDK client, which is what integrators actually use
    c = NeuralOSDClient(base, token=TOKEN)
    r = c.ask("how many rows")
    assert r.results[0]["count"] == 4
