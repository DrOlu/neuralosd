"""The agentic contracts: policy, budgets, sessions, traces, HITL, mesh and
the builder tools.

These are the pieces a human CLI does not need: an agent has an identity, a
call budget, a session, a run and a retry story. Each test pins one contract
so the MCP and HTTP surfaces cannot quietly drift apart.
"""
import json

import pytest

import neuralosd.mcp as mcp
from neuralosd.mcp import handle

PROBES = '''\
from neuralosd import probe


@probe(description="How many rows are in the table",
       triggers=["how many rows", "row count", "count rows"])
def count_rows():
    return {"count": 3}


@probe(description="Contact email",
       triggers=["contact email", "support email"])
def contact_email():
    return {"email": "agent@example.com"}


@probe(description="Dangerous reset",
       triggers=["reset everything", "wipe the table"], confirm=True)
def reset_everything():
    return {"reset": True}
'''


@pytest.fixture
def inst_dir(tmp_path):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text(PROBES, encoding="utf-8")
    return d


def _call(inst_dir, name, arguments=None):
    req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": name, "arguments": arguments or {}}}
    return handle(req, str(inst_dir))["result"]


def _acl(inst_dir, agents):
    (inst_dir / "acl.json").write_text(json.dumps({"agents": agents}),
                                       encoding="utf-8")


def _audit_rows(inst_dir):
    path = inst_dir / "ask_audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ── identity / policy ────────────────────────────────────────────────────

def test_acl_is_an_allow_list_once_agents_is_declared(inst_dir):
    _acl(inst_dir, {"planner": {}})
    denied = _call(inst_dir, "ask",
                   {"question": "how many rows", "agent_id": "intruder"})
    assert denied["isError"] is True
    assert denied["structuredContent"]["code"] == "policy_denied"

    allowed = _call(inst_dir, "ask",
                    {"question": "how many rows", "agent_id": "planner"})
    assert allowed["isError"] is False


def test_no_agents_block_keeps_backward_compatibility(inst_dir):
    out = _call(inst_dir, "ask", {"question": "how many rows"})
    assert out["isError"] is False


def test_per_agent_pii_policy(inst_dir):
    _acl(inst_dir, {"reader": {"pii": False}})
    masked = _call(inst_dir, "ask",
                   {"question": "contact email", "agent_id": "reader"})
    env = masked["structuredContent"]
    # reader is allowed to see PII, but the audit row is still masked
    assert env["results"][0]["email"] == "agent@example.com"
    row = _audit_rows(inst_dir)[-1]
    assert row["results"][0]["email"] != "agent@example.com"

    _acl(inst_dir, {"reader": {"pii": True}})
    masked = _call(inst_dir, "ask",
                   {"question": "contact email", "agent_id": "reader"})
    assert masked["structuredContent"]["results"][0]["email"] != \
        "agent@example.com"


def test_per_agent_budget(inst_dir):
    _acl(inst_dir, {"spender": {"budget": {"calls": 1, "window": 3600}}})
    first = _call(inst_dir, "ask",
                  {"question": "how many rows", "agent_id": "spender"})
    assert first["isError"] is False
    second = _call(inst_dir, "ask",
                   {"question": "how many rows", "agent_id": "spender"})
    assert second["isError"] is True
    assert second["structuredContent"]["code"] == "budget_exceeded"
    assert second["structuredContent"]["retry_after"] >= 1


# ── sessions ─────────────────────────────────────────────────────────────

def test_session_records_turns_and_previous_question(inst_dir):
    first = _call(inst_dir, "ask",
                  {"question": "how many rows", "session_id": "s-1"})
    assert first["structuredContent"]["_meta"]["session"]["turn"] == 1
    second = _call(inst_dir, "ask",
                   {"question": "row count", "session_id": "s-1"})
    meta = second["structuredContent"]["_meta"]["session"]
    assert meta["turn"] == 2
    assert meta["previous"] == "how many rows"

    last = _call(inst_dir, "memory", {"op": "last", "session_id": "s-1"})
    assert last["structuredContent"]["last"]["text"] == "row count"
    turns = _call(inst_dir, "memory", {"op": "turns", "session_id": "s-1"})
    assert turns["structuredContent"]["turns"] == 2
    recall = _call(inst_dir, "memory",
                   {"op": "recall", "session_id": "s-1", "query": "rows"})
    assert recall["structuredContent"]["results"]


def test_memory_remember(inst_dir):
    out = _call(inst_dir, "memory",
                {"op": "remember", "session_id": "s-2",
                 "text": "the user prefers region=EMEA"})
    assert out["isError"] is False
    last = _call(inst_dir, "memory", {"op": "last", "session_id": "s-2"})
    assert "EMEA" in last["structuredContent"]["last"]["text"]


# ── tracing ──────────────────────────────────────────────────────────────

def test_run_trace_spans_two_asks(inst_dir):
    _call(inst_dir, "ask", {"question": "how many rows", "run_id": "r-1"})
    _call(inst_dir, "ask", {"question": "row count", "run_id": "r-1"})
    trace = _call(inst_dir, "trace", {"op": "read", "run_id": "r-1"})
    events = trace["structuredContent"]["events"]
    assert len(events) == 2
    assert all(e["tool"] == "ask" for e in events)
    assert all(e["probe"] == "count_rows" for e in events)
    runs = _call(inst_dir, "trace", {"op": "runs"})
    assert "r-1" in runs["structuredContent"]["runs"]


# ── HITL ─────────────────────────────────────────────────────────────────

def test_confirm_probe_pauses_then_executes(inst_dir):
    pending = _call(inst_dir, "ask", {"question": "reset everything"})
    env = pending["structuredContent"]
    assert pending["isError"] is False
    assert env["mode"] == "pending_confirmation"
    token = env["confirm_token"]
    assert token

    # it did NOT execute: no result envelope and no audit row yet
    assert not (inst_dir / "ask_audit.jsonl").exists()

    done = _call(inst_dir, "confirm", {"token": token})
    assert done["isError"] is False
    assert done["structuredContent"]["results"][0]["reset"] is True
    assert done["structuredContent"]["mode"] == "confirmed"
    assert _audit_rows(inst_dir)[-1]["mode"] == "confirmed"


def test_invalid_confirm_token_is_an_error(inst_dir):
    out = _call(inst_dir, "confirm", {"token": "nope"})
    assert out["isError"] is True
    assert out["structuredContent"]["code"] == "invalid"


def test_confirm_token_is_single_use(inst_dir):
    pending = _call(inst_dir, "ask", {"question": "reset everything"})
    token = pending["structuredContent"]["confirm_token"]
    first = _call(inst_dir, "confirm", {"token": token})
    assert first["isError"] is False
    second = _call(inst_dir, "confirm", {"token": token})
    assert second["isError"] is True
    assert second["structuredContent"]["code"] == "already_used"


# ── builder tools ────────────────────────────────────────────────────────

def test_build_tool_calls_the_verified_builder(inst_dir, monkeypatch):
    seen = {}

    def fake(description, instance_dir, model=None, provider=None,
             timeout=900):
        seen["description"] = description
        return {"built": True, "restored": False}

    monkeypatch.setattr(mcp, "_build_capability", fake)
    out = _call(inst_dir, "build", {"description": "average rows"})
    assert out["isError"] is False
    assert out["structuredContent"]["built"] is True
    assert seen["description"] == "average rows"


def test_shadow_report_and_mothball_dry_run(inst_dir):
    report = _call(inst_dir, "shadow", {"op": "report"})
    assert report["isError"] is False
    assert "shadowed" in report["structuredContent"]

    plan = _call(inst_dir, "mothball", {"age_days": 0, "apply": False})
    assert plan["isError"] is False
    assert "candidates" in plan["structuredContent"]
    assert not (inst_dir / "mothballed.json").exists()


# ── mesh ─────────────────────────────────────────────────────────────────

class _FakeTransport:
    def __init__(self, agents):
        self._agents = agents

    def discover(self, skill=""):
        return self._agents


def test_discover_ranks_peers(inst_dir, monkeypatch):
    peer = {"agent_id": "peer-a", "skills": ["count_rows"],
            "capabilities": [{"name": "count_rows",
                              "description": "row counts",
                              "triggers": ["how many rows"]}]}
    monkeypatch.setattr(mcp, "_mesh_transport",
                        lambda gateway=None: _FakeTransport([peer]))
    out = _call(inst_dir, "discover", {"question": "how many rows"})
    peers = out["structuredContent"]["peers"]
    assert peers[0]["agent_id"] == "peer-a"


def test_delegate_unknown_peer_is_an_error(inst_dir, monkeypatch):
    monkeypatch.setattr(mcp, "_mesh_transport",
                        lambda gateway=None: _FakeTransport([]))
    out = _call(inst_dir, "delegate",
                {"peer": "ghost", "question": "how many rows"})
    assert out["isError"] is True
    assert "peer not found" in out["structuredContent"]["error"]


# ── CLI ──────────────────────────────────────────────────────────────────

def test_cli_explain_is_a_dry_run(inst_dir):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "neuralosd", "ask",
         "--instance-dir", str(inst_dir), "--explain", "how many rows"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["probe"] == "count_rows"
    assert out["would_answer"] is True
    assert not (inst_dir / "ask_audit.jsonl").exists()
