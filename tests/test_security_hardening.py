"""Security contracts for the agentic surface.

The threats here are identity spoofing, cross-agent data access, wide
binding without a credential, filename traversal through a label/name, and
command injection through a deploy directory. Each must fail closed.
"""
import hashlib
import inspect
import json
import shlex
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from neuralosd._cmd import deploy as deploy_mod
from neuralosd.agentic import effective_role, hash_token
from neuralosd.loop import pin_snapshot
from neuralosd.multi import MultiInstanceServer, serve_multi
from neuralosd.probe import probe as probe_dec
from neuralosd.router import Router
from neuralosd.scheduler import Scheduler
from neuralosd.service import serve

PROBES = '''\
from neuralosd import probe


@probe(description="How many rows", triggers=["how many rows"])
def count_rows():
    return {"count": 3}
'''


def _probe(name, triggers, fn):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "),
                     triggers=triggers)(fn)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(base, method, path, body=None, token=None, agent=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if agent:
        headers["X-Agent-Id"] = agent
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def live(tmp_path):
    (tmp_path / "acl.json").write_text(json.dumps({
        "agents": {
            "alice": {"token_sha256": hash_token("alice-secret")},
            "bob": {"token_sha256": hash_token("bob-secret")},
        }
    }), encoding="utf-8")
    router = Router(
        probes=[_probe("count_rows", ["how many rows"],
                       lambda **_k: [{"n": 3}])],
        cache_file=str(tmp_path / "c.json"),
        audit_file=str(tmp_path / "a.jsonl"),
    )
    port = _free_port()
    threading.Thread(target=serve, args=(router,),
                     kwargs={"port": port, "host": "127.0.0.1"},
                     daemon=True).start()
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


# ── binding ──────────────────────────────────────────────────────────────

def test_serve_defaults_to_loopback():
    assert inspect.signature(serve).parameters["host"].default == "127.0.0.1"


def test_serve_refuses_wide_bind_without_a_credential(tmp_path):
    router = Router(probes=[], cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    with pytest.raises(SystemExit) as exc:
        serve(router, host="0.0.0.0", port=0)
    assert "refusing to bind" in str(exc.value)


def test_multi_refuses_wide_bind_without_a_token(tmp_path):
    server = MultiInstanceServer(instances_dir=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        serve_multi(server, host="0.0.0.0", port=0)
    assert "refusing to bind" in str(exc.value)


# ── agent identity ───────────────────────────────────────────────────────

def test_request_without_a_token_is_401(live):
    base, _ = live
    code, _ = _request(base, "POST", "/ask", {"question": "how many rows"})
    assert code == 401


def test_agent_token_binds_the_identity(live):
    base, tmp_path = live
    code, body = _request(base, "POST", "/ask",
                          {"question": "how many rows"},
                          token="alice-secret")
    assert code == 200
    assert body["_meta"]["agent_id"] == "alice"
    rows = [json.loads(line) for line in
            (tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert rows[-1]["agent_id"] == "alice"


def test_agent_cannot_claim_another_identity(live):
    base, _ = live
    code, body = _request(base, "POST", "/ask",
                          {"question": "how many rows"},
                          token="alice-secret", agent="bob")
    assert code == 403
    assert body["code"] == "agent_mismatch"


def test_wrong_token_is_401(live):
    base, _ = live
    code, _ = _request(base, "POST", "/ask",
                       {"question": "how many rows"}, token="wrong")
    assert code == 401


def test_agent_token_cannot_escalate_its_role():
    # the agent's role comes from its ACL entry, never from the header
    assert effective_role("admin", None, "agent") is None
    assert effective_role("admin", "analyst", "agent") == "analyst"
    # the operator token (and the no-auth loopback mode) may select a role
    assert effective_role("admin", None, "operator") == "admin"
    assert effective_role(None, "analyst", "open") == "analyst"


def test_agent_sees_only_its_own_audit_rows(live):
    base, tmp_path = live
    _request(base, "POST", "/ask", {"question": "how many rows"},
             token="alice-secret")
    _request(base, "POST", "/ask", {"question": "how many rows"},
             token="bob-secret")
    code, body = _request(base, "GET", "/audit", token="alice-secret")
    assert code == 200
    assert body["rows"]
    assert {row["agent_id"] for row in body["rows"]} == {"alice"}


def test_agent_cannot_read_another_agents_run(live):
    base, _ = live
    code, body = _request(base, "POST", "/ask",
                          {"question": "how many rows", "async": True},
                          token="bob-secret")
    assert code == 202
    run_id = body["run_id"]

    code, _ = _request(base, "GET", f"/runs/{run_id}", token="alice-secret")
    assert code == 404
    code, run = _request(base, "GET", f"/runs/{run_id}", token="bob-secret")
    assert code == 200
    assert run["meta"]["agent_id"] == "bob"


# ── filenames ────────────────────────────────────────────────────────────

def test_snapshot_label_cannot_traverse(tmp_path):
    with pytest.raises(SystemExit):
        pin_snapshot(str(tmp_path), "../escape")
    with pytest.raises(SystemExit):
        pin_snapshot(str(tmp_path), "..")


def test_schedule_name_cannot_traverse(tmp_path):
    scheduler = Scheduler(str(tmp_path), lambda _i, _q: {})
    with pytest.raises(ValueError):
        scheduler.add("../escape", 60, "inst", "q")
    with pytest.raises(ValueError):
        scheduler.add("a/b", 60, "inst", "q")


# ── deploy command construction ──────────────────────────────────────────

def test_boxlite_start_script_is_not_a_shell_string(tmp_path):
    nasty = '/tmp/a"b$(id); echo pwned'
    script = deploy_mod._boxlite_start_script(nasty, 8877)
    compile(script, "<start>", "exec")        # it must be valid Python
    assert "subprocess.Popen" in script
    assert "os.system" not in script
    assert "--host', '127.0.0.1'" in script      # loopback only
    assert "0.0.0.0" not in script


def test_msb_start_command_quotes_the_directory():
    nasty = "/tmp/a b; echo pwned"
    cmd = deploy_mod._msb_start_command(nasty, 8877)
    assert "127.0.0.1" in cmd
    assert "0.0.0.0" not in cmd
    # the directory survives as ONE shell word; the semicolon is data
    assert nasty in shlex.split(cmd)
