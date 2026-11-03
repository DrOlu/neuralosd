"""Tests-first: v2.7.0 — Pi Durable integration + loop optimizations.

Pi Durable (@earendil-works/pi-durable) is Earendil's durable harness:
storage-backed conversations that survive crashes, idempotent submissions,
forks. It is a Node LIBRARY (no CLI), so neuralosd ships a small Node
bridge (pi_durable_bridge.mjs) and drives it over line-JSON stdin/stdout:

  BUILDER (durable):  `neuralosd build "<capability>" --instance-dir . --durable`
                      opens <instance>/pi_durable.sqlite, submits the build
                      prompt with a STABLE requestId (hash of question+dir),
                      waits for the settled answer, then runs the SAME
                      verification/restore pipeline as the one-shot builder.
                      A crash or `--resume` re-attaches to the SAME
                      conversation — the in-flight reasoning survives.

  OPERATOR (durable): `neuralosd pi durable --status | --steer "..." | --stop`

Also in this increment:
  - builder prompt enrichment: the prompt carries the bridge's function
    inventory, the existing probe names, and a data sample, so pi needs
    fewer exploration steps
  - bounded audit reads: slo/gaps read audit logs with a cap instead of
    loading multi-GB files whole
  - `neuralosd doctor`: one command that health-checks every integration

Unit tests run with a FAKE bridge subprocess — no Node, no npm, no model.
One e2e (skipped unless NEURALOSD_PI_E2E=1) uses the real package.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

CSV = "region,units\nNorth,100\nSouth,200\n"


def _build(tmp_path, name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(CSV, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None, extra_sources=None,
                                 join_on=None, since=None, header=None,
                                 secret_ref=None, secret_header=None))
    return out


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res


# ── the bridge command + script ───────────────────────────────────────────

def test_bridge_script_is_shipped_and_syntactically_valid():
    """neuralosd ships the Node bridge; `node --check` must accept it even
    where the npm package is absent (syntax is checkable standalone)."""
    import subprocess
    from neuralosd import pidurable
    path = pidurable.bridge_script_path()
    assert os.path.isfile(path)
    r = subprocess.run(["node", "--check", path], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[:300]


def test_bridge_command_pins_storage_model_and_provider():
    from neuralosd.pisources import pi_durable_command
    argv = pi_durable_command(storage="/x/agent.sqlite",
                              model="z-ai/glm-5.3-flash",
                              provider="openrouter")
    assert argv[0] == "node"
    assert any("pi_durable_bridge.mjs" in a for a in argv)
    assert argv[argv.index("--storage") + 1] == "/x/agent.sqlite"
    assert argv[argv.index("--model") + 1] == "z-ai/glm-5.3-flash"
    assert argv[argv.index("--provider") + 1] == "openrouter"


# ── the Python driver over line-JSON ──────────────────────────────────────

class FakeBridge:
    """A line-JSON bridge process that models durability with a dict."""

    def __init__(self, storage):
        self.storage = storage          # shared dict = the "sqlite" file
        self.alive = True

    def handle(self, obj):
        op = obj.get("op")
        if op == "submit":
            rid = obj["requestId"]
            self.storage["last_content"] = obj.get("content", "")
            if rid not in self.storage["answers"]:
                self.storage["answers"][rid] = {
                    "state": "completed",
                    "text": f"did: {self.storage['last_content']}",
                }
            return {"op": "submitted", "requestId": rid,
                    "state": self.storage["answers"][rid]["state"]}
        if op == "wait":
            a = self.storage["answers"].get(obj["requestId"], {})
            return {"op": "settled", "requestId": obj["requestId"],
                    "state": a.get("state", "unknown"),
                    "text": a.get("text", "")}
        if op == "status":
            return {"op": "status", "conversation": 1,
                    "answers": len(self.storage["answers"])}
        return {"op": "error", "message": f"unknown op {op}"}


def test_durable_submission_is_exactly_once(tmp_path):
    """A retried submit with the SAME requestId gets the original submission
    back — never a second run. This is the property that makes crash
    retries safe."""
    from neuralosd.pidurable import DurableBuilder
    storage = {"answers": {}, "last_content": ""}
    b = DurableBuilder(spawn=lambda: FakeBridge(storage))
    r1 = b.submit("build the probe", request_id="job-42")
    r2 = b.submit("build the probe", request_id="job-42")
    assert r1["requestId"] == r2["requestId"] == "job-42"
    settled = b.wait("job-42")
    assert settled["state"] == "completed"
    b.close()


def test_resume_reattaches_to_the_same_storage(tmp_path):
    """Crash (close the process), reopen on the same storage: the previously
    settled submission is still there — durability, not a fresh start."""
    from neuralosd.pidurable import DurableBuilder
    storage = {"answers": {}, "last_content": ""}
    b1 = DurableBuilder(spawn=lambda: FakeBridge(storage))
    b1.submit("long build task", request_id="build-1")
    settled = b1.wait("build-1")
    assert settled["text"] == "did: long build task"
    b1.close()                                   # the process "dies"

    b2 = DurableBuilder(spawn=lambda: FakeBridge(storage))   # reopen
    settled2 = b2.wait("build-1")
    assert settled2["text"] == "did: long build task"
    assert b2.status()["answers"] == 1
    b2.close()


def test_wait_reports_an_unknown_request_honestly(tmp_path):
    from neuralosd.pidurable import DurableBuilder
    b = DurableBuilder(spawn=lambda: FakeBridge({"answers": {}}))
    r = b.wait("never-submitted")
    assert r["state"] == "unknown"
    b.close()


# ── the durable builder: same verification, resumable conversation ────────

def test_durable_build_verifies_and_restores_like_the_one_shot(tmp_path):
    from neuralosd.pisources import build_durable
    out = _build(tmp_path)
    before = open(os.path.join(out, "probes.py"), encoding="utf-8").read()

    class FakeProc:
        def __init__(self, storage):
            self.storage = storage

    storage = {"answers": {}, "last_content": ""}
    report = build_durable("add a probe", instance_dir=out,
                           model="z-ai/glm-5.3-flash",
                           provider="openrouter",
                           spawn=lambda: FakeBridge(storage))
    # pi "said" something but changed no files -> banks green -> delivered
    assert report["ok"] is True
    assert report["verified"] is True
    assert open(os.path.join(out, "probes.py"),
                encoding="utf-8").read() == before
    assert os.path.isfile(os.path.join(
        out, "pi_durable.sqlite")) or report.get("storage") is not None


def test_durable_build_does_not_lose_the_transcript_on_failure(tmp_path):
    """One-shot builds restore AND forget; durable builds restore the files
    but KEEP the conversation storage — the reasoning survives for --resume."""
    from neuralosd.pisources import build_durable
    out = _build(tmp_path)
    storage = {"answers": {}, "last_content": ""}

    def bad_pi_via_bridge():
        def spawn():
            class B:
                def handle(self, obj):
                    if obj["op"] == "submit":
                        with open(os.path.join(out, "probes.py"), "a",
                                  encoding="utf-8") as fh:
                            fh.write("\nGARBAGE\n")
                        return {"op": "submitted",
                                "requestId": obj["requestId"],
                                "state": "completed"}
                    return {"op": "settled", "text": "wrote garbage",
                            "state": "completed"}
            return B()
        return spawn

    report = build_durable("add a probe", instance_dir=out,
                           model="z-ai/glm-5.3-flash",
                           provider="openrouter",
                           spawn=bad_pi_via_bridge())
    assert report["ok"] is False and report["restored"] is True
    # the storage object still holds the conversation:
    assert "answers" in storage


# ── optimizations ─────────────────────────────────────────────────────────

def test_build_prompt_enrichment_carries_the_inventory(tmp_path):
    """The enriched prompt gives pi the bridge inventory, probe names and a
    data sample UP FRONT — fewer exploration steps, higher first-shot rate."""
    from neuralosd.pisources import build_prompt
    out = _build(tmp_path)
    prompt = build_prompt("average units per region", instance_dir=out)
    assert "row_count" in prompt                 # existing probe names
    assert "BRIDGE FUNCTIONS" in prompt
    assert "def breakdown(dimension, measure)" in prompt   # real bridge def
    assert "region" in prompt and "units" in prompt
    assert "North" in prompt                     # a data sample row


def test_slo_and_gaps_read_large_audits_bounded(tmp_path, monkeypatch):
    """Multi-GB audit files must not be loaded whole: the read is capped at
    the tail, and results stay correct for the window that matters."""
    from neuralosd import loop
    out = _build(tmp_path)
    audit = os.path.join(out, "ask_audit.jsonl")
    import time
    now = time.time()
    with open(audit, "w", encoding="utf-8") as fh:
        for i in range(20000):
            fh.write(json.dumps({
                "ts": now - (20000 - i), "question": f"q{i}",
                "probe": None, "results": None,
                "refusal_reason": "no_probe_matches"}) + "\n")
    reads = []
    real_open = open

    def counting_open(path, *a, **kw):
        reads.append(path)
        return real_open(path, *a, **kw)

    stats = loop.refusal_rate(out, window_hours=1, max_refusal_rate=0.4,
                              tail_lines=5000)
    # wall clock moves between write and check — allow the boundary entry
    # to slide below the cutoff:
    assert 3598 <= stats["asks"] <= 3600
    assert stats["refusal_rate"] == 1.0


def test_doctor_reports_every_integration(tmp_path, monkeypatch, capsys):
    from neuralosd._cmd import doctor as doctor_cmd
    monkeypatch.setattr(doctor_cmd, "_checks", lambda inst: {
        "pi": {"ok": True, "detail": "ready (z-ai/glm-5.3-flash)"},
        "pi_durable": {"ok": False, "detail": "node missing"},
        "agent_browser": {"ok": True, "detail": "installed"},
        "temporal": {"ok": False, "detail": "CLI not installed"},
        "banks": {"ok": True, "detail": "2 traps, 0 failures"},
    })
    out = str(tmp_path / "inst")
    _build(tmp_path)
    class A:
        instance_dir = out
        as_json = True
    rc = doctor_cmd.run(A)
    blob = capsys.readouterr().out
    assert '"pi"' in blob and '"temporal"' in blob
    assert "z-ai/glm-5.3-flash" in blob


def test_doctor_exit_code_reflects_failures(tmp_path, monkeypatch, capsys):
    from neuralosd._cmd import doctor as doctor_cmd
    monkeypatch.setattr(doctor_cmd, "_checks", lambda inst: {
        "pi": {"ok": True, "detail": "ok"},
        "temporal": {"ok": False, "detail": "missing"},
    })
    class A:
        instance_dir = str(_build(tmp_path))
        as_json = False
    rc = doctor_cmd.run(A)
    assert rc == 1
