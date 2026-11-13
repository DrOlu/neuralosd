"""The HTTP agent surface: identity, policy, budgets, idempotency, async
runs, HITL and the refusal contract — tested on a real socket.

These promises are made to another process, so they are tested over HTTP,
not against in-process helpers.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import Router
from neuralosd.service import serve


def _probe(name, triggers, fn, confirm=False):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     confirm=confirm)(fn)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Authorization": "Bearer tok", "Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def live(tmp_path):
    router = Router(
        probes=[
            _probe("count_rows", ["how many rows"], lambda **_k: [{"n": 3}]),
            _probe("contact_email", ["contact email"],
                   lambda **_k: [{"email": "agent@example.com"}]),
            _probe("reset_everything", ["reset everything"],
                   lambda **_k: [{"reset": True}], confirm=True),
        ],
        cache_file=str(tmp_path / "c.json"),
        audit_file=str(tmp_path / "a.jsonl"),
    )
    port = _free_port()
    threading.Thread(target=serve, args=(router,),
                     kwargs={"port": port, "host": "127.0.0.1",
                             "auth_token": "tok"}, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    else:                                             # pragma: no cover
        pytest.fail("server did not start")
    return base, tmp_path


def _acl(tmp_path, agents):
    (tmp_path / "acl.json").write_text(json.dumps({"agents": agents}),
                                       encoding="utf-8")


def _audit(tmp_path):
    path = tmp_path / "a.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_answer_carries_trust_and_provenance(live):
    base, _ = live
    code, body = _request(base, "POST", "/ask", {"question": "how many rows"})
    assert code == 200
    assert body["content_trust"] == "untrusted"
    assert body["provenance"]["probe"] == "count_rows"
    assert body["_meta"]["instance"] == "instance"


def test_refusal_has_a_machine_readable_contract(live):
    base, _ = live
    code, body = _request(base, "POST", "/ask",
                          {"question": "quantum entanglement statistics"})
    assert code == 422
    assert body["refusal"]["code"] == "no_probe_matches"
    assert body["refusal"]["retryable"] is True
    assert body["content_trust"] == "untrusted"


def test_agent_session_and_run_are_stamped_on_the_audit_row(live):
    base, tmp_path = live
    headers = {"X-Agent-Id": "planner", "X-Session-Id": "s-1",
               "X-Run-Id": "r-1"}
    code, _ = _request(base, "POST", "/ask",
                       {"question": "how many rows"}, headers=headers)
    assert code == 200
    rows = _audit(tmp_path)
    assert rows[-1]["agent_id"] == "planner"
    assert rows[-1]["session_id"] == "s-1"
    assert rows[-1]["run_id"] == "r-1"

    code, body = _request(base, "GET", "/audit?run_id=r-1")
    assert code == 200
    assert len(body["rows"]) == 1


def test_acl_is_an_allow_list(live):
    base, tmp_path = live
    _acl(tmp_path, {"planner": {}})
    code, body = _request(base, "POST", "/ask",
                          {"question": "how many rows"},
                          headers={"X-Agent-Id": "intruder"})
    assert code == 403
    assert body["code"] == "policy_denied"

    code, _ = _request(base, "POST", "/ask",
                       {"question": "how many rows"},
                       headers={"X-Agent-Id": "planner"})
    assert code == 200


def test_per_agent_pii_policy(live):
    base, tmp_path = live
    _acl(tmp_path, {"reader": {"pii": False}})
    code, body = _request(base, "POST", "/ask",
                          {"question": "contact email"},
                          headers={"X-Agent-Id": "reader"})
    assert code == 200
    assert body["results"][0]["email"] == "agent@example.com"
    # the audit row is still masked, whatever the reader may see
    assert _audit(tmp_path)[-1]["results"][0]["email"] != "agent@example.com"


def test_per_agent_budget_returns_429(live):
    base, tmp_path = live
    _acl(tmp_path, {"spender": {"budget": {"calls": 1, "window": 3600}}})
    headers = {"X-Agent-Id": "spender"}
    code, _ = _request(base, "POST", "/ask",
                       {"question": "how many rows"}, headers=headers)
    assert code == 200
    code, body = _request(base, "POST", "/ask",
                          {"question": "how many rows"}, headers=headers)
    assert code == 429
    assert body["code"] == "budget_exceeded"
    assert body["retry_after"] >= 1


def test_idempotency_key_replays_the_first_result(live):
    base, tmp_path = live
    headers = {"Idempotency-Key": "k-1"}
    code1, body1 = _request(base, "POST", "/ask",
                            {"question": "how many rows"}, headers=headers)
    code2, body2 = _request(base, "POST", "/ask",
                            {"question": "how many rows"}, headers=headers)
    assert (code1, code2) == (200, 200)
    assert body2["ask_id"] == body1["ask_id"]
    assert body2["_meta"]["replayed"] is True
    assert len(_audit(tmp_path)) == 1


def test_async_ask_and_poll(live):
    base, _ = live
    code, body = _request(base, "POST", "/ask",
                          {"question": "how many rows", "async": True})
    assert code == 202
    run_id = body["run_id"]
    assert body["status"] == "queued"

    for _ in range(100):
        code, run = _request(base, "GET", f"/runs/{run_id}")
        if run["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert code == 200
    assert run["status"] == "done"
    assert run["result"]["results"][0]["n"] == 3


def test_hitl_confirm_endpoint(live):
    base, tmp_path = live
    code, pending = _request(base, "POST", "/ask",
                             {"question": "reset everything"})
    assert code == 202
    assert pending["mode"] == "pending_confirmation"
    token = pending["confirm_token"]
    assert token
    assert not _audit(tmp_path)

    code, done = _request(base, "POST", f"/v1/confirm/{token}")
    assert code == 200
    assert done["results"][0]["reset"] is True
    assert done["mode"] == "confirmed"
    assert _audit(tmp_path)[-1]["mode"] == "confirmed"


def test_tools_endpoint_matches_the_mcp_surface(live):
    base, _ = live
    code, body = _request(base, "GET", "/tools")
    assert code == 200
    names = [t["name"] for t in body["tools"]]
    assert names[0] == "ask"
    assert "confirm" in names
    assert "build" in names
