"""Production-hardening edge cases for the agentic surface.

These are the cases a commercial deployment meets: a caller-supplied session
id that tries to walk the filesystem, a malformed ACL budget, an agent that
is allowed to ask but not to mutate the instance, a huge HTTP body, and a
ledger query with a junk limit. Each must degrade to a clear, bounded
answer — never a 500, a traceback, or a path outside the store.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import neuralosd.mcp as mcp
from neuralosd.agentic import (BudgetStore, SessionStore, resolve_agent)
from neuralosd.mcp import handle
from neuralosd.probe import probe as probe_dec
from neuralosd.router import Router
from neuralosd.service import serve


# ── session names ────────────────────────────────────────────────────────

def test_session_id_cannot_escape_the_memory_directory(tmp_path):
    store = SessionStore(str(tmp_path))
    store.remember("../../etc/passwd", "not a traversal")
    assert store.turns("../../etc/passwd") == 1
    path = store._memory.path("../../etc/passwd")
    memory_dir = tmp_path / ".agentic" / "memory"
    assert str(memory_dir) in path
    files = list(memory_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].parent == memory_dir
    assert not (tmp_path / "etc").exists()


def test_sanitised_session_ids_do_not_collide(tmp_path):
    store = SessionStore(str(tmp_path))
    a = store._memory.path("a/b")
    b = store._memory.path("a_b")
    assert a != b


def test_memory_store_is_bounded(tmp_path):
    from neuralosd.memory import MemoryStore

    store = MemoryStore(str(tmp_path), max_records=3)
    for i in range(5):
        store.remember("s", f"note {i}")
    rows = store._read_rows(store.path("s"))
    assert [r["text"] for r in rows] == ["note 2", "note 3", "note 4"]
    assert len(store.recall("s", "note", k=10)) == 3


# ── policy resolution ────────────────────────────────────────────────────

def test_malformed_budget_does_not_raise(tmp_path):
    (tmp_path / "acl.json").write_text(json.dumps({
        "agents": {"a": {"budget": {"calls": "many", "window": -5}}}
    }), encoding="utf-8")
    policy = resolve_agent(str(tmp_path), "a")
    assert policy["allowed"] is True
    assert policy["budget"] == {"calls": 0, "window": 3600}


def test_non_string_agent_id_is_denied(tmp_path):
    (tmp_path / "acl.json").write_text(json.dumps({
        "agents": {"a": {}}
    }), encoding="utf-8")
    assert resolve_agent(str(tmp_path), {"not": "a string"})["allowed"] is False
    assert resolve_agent(str(tmp_path), None)["allowed"] is False


def test_budget_store_is_bounded():
    store = BudgetStore(max_agents=5)
    for i in range(50):
        store.check(f"agent-{i}", 10, 60)
    assert len(store._hits) <= 5


# ── admin gating ─────────────────────────────────────────────────────────

PROBES = '''\
from neuralosd import probe


@probe(description="How many rows", triggers=["how many rows"])
def count_rows():
    return {"count": 3}
'''


@pytest.fixture
def inst_dir(tmp_path):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text(PROBES, encoding="utf-8")
    return d


def _call(inst_dir, name, arguments=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": arguments or {}}}
    return handle(req, str(inst_dir))["result"]


def test_mutating_tools_require_admin_when_an_acl_exists(inst_dir, monkeypatch):
    (inst_dir / "acl.json").write_text(json.dumps({
        "agents": {"worker": {}}
    }), encoding="utf-8")
    monkeypatch.setattr(mcp, "_build_capability",
                        lambda *a, **k: {"built": True})

    for tool, args in (
            ("build", {"description": "average rows", "agent_id": "worker"}),
            ("shadow", {"op": "evaluate", "name": "m", "agent_id": "worker"}),
            ("mothball", {"apply": False, "agent_id": "worker"}),
    ):
        result = _call(inst_dir, tool, args)
        assert result["isError"] is True, tool
        assert result["structuredContent"]["code"] == "policy_denied", tool

    (inst_dir / "acl.json").write_text(json.dumps({
        "agents": {"worker": {"admin": True}}
    }), encoding="utf-8")
    allowed = _call(inst_dir, "build",
                    {"description": "average rows", "agent_id": "worker"})
    assert allowed["isError"] is False
    assert allowed["structuredContent"]["built"] is True


def test_mutating_tools_are_trusted_when_no_acl_exists(inst_dir, monkeypatch):
    monkeypatch.setattr(mcp, "_build_capability",
                        lambda *a, **k: {"built": True})
    result = _call(inst_dir, "build", {"description": "average rows"})
    assert result["isError"] is False


def test_confirmed_probe_removed_before_confirmation_refuses(tmp_path):
    def risky(**_kw):
        return {"ok": True}

    risky.__name__ = "risky"
    router = Router(probes=[probe_dec(description="risky",
                                      triggers=["risky"],
                                      confirm=True)(risky)],
                    cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    meta = router.metas[0]
    router.by_name.pop(meta.name)
    env = router._execute_confirmed(meta, {}, "risky", "risky", "ask-1")
    assert env["mode"] == "refused"
    assert env["refusal_reason"] == "no_probe_matches"
    assert "no longer in the menu" in env["error"]


# ── HTTP edge cases ──────────────────────────────────────────────────────

def _probe(name, triggers, fn):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "),
                     triggers=triggers)(fn)


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
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def live(tmp_path):
    router = Router(
        probes=[_probe("count_rows", ["how many rows"],
                       lambda **_k: [{"n": 3}])],
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
    return base


def test_oversized_body_is_413(live):
    code, body = _request(live, "POST", "/ask",
                          {"question": "x" * 1_000_001})
    assert code == 413
    assert "too large" in body["error"]


def test_junk_audit_limit_is_200(live):
    code, body = _request(live, "GET", "/audit?limit=abc")
    assert code == 200
    assert body["rows"] == []
