"""Tests-first: v2.9.0 — Plasma EDR integration (all four layers).

  L1  `plasma:` source connector — reads events.jsonl (the strict
      PlasmaEvent contract), normalizes to security events, profiles them
      for the standard probe pipeline.
  L2  Security-specific probes: `critical_alerts`, `failed_logons`,
      `new_scheduled_tasks`, `events_by_type` — generated for plasma
      sources, with misuse traps born at probe registration.
  L3  The verified-answer loop works on security data: golden bank,
      gap clustering, oracle receipts — all on Plasma telemetry.
  L4  Actuation with proof: named, journaled, reversible actions
      (block_ip, isolate_host, disable_user) — dry-run first, --apply
      requires a plan id, every action journaled with who/when/what/why.
      The actual system change is behind an injectable interface —
      tests never touch a real system.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance


# ── PlasmaEvent fixtures (the strict contract from events.jsonl) ─────────

WHIDS_HIT = {
    "ts": "2026-11-04T09:00:00Z", "host": "WS1",
    "source": "whids", "event_type": "whids_detect",
    "severity": "high", "technique": "T1059.001",
    "process": "powershell.exe", "account": "CORP\\admin",
    "message": "Suspicious PowerShell: Invoke-Mimikatz",
}

TAP_MATCH = {
    "ts": "2026-11-04T09:05:00Z", "host": "WS1",
    "source": "tap", "event_type": "tap_match",
    "severity": "medium", "provider": "powershell",
    "process": "powershell.exe", "account": "CORP\\admin",
    "message": "ScriptBlock: DownloadString",
}

LOGON_FAIL = {
    "ts": "2026-11-04T09:10:00Z", "host": "WS1",
    "source": "identity", "event_type": "logon_fail",
    "severity": "info", "account": "CORP\\admin",
    "message": "4625 failed logon",
}

SCHTASK = {
    "ts": "2026-11-04T09:15:00Z", "host": "WS1",
    "source": "tap", "event_type": "schtask_create",
    "severity": "high", "process": "schtasks.exe",
    "account": "CORP\\admin", "message": "New task: persist",
}

INFO_EVENT = {
    "ts": "2026-11-04T09:20:00Z", "host": "WS1",
    "source": "identity", "event_type": "logon_ok",
    "severity": "info", "account": "CORP\\ola",
    "message": "4624 successful logon",
}


def _write_events(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return str(path)


def _build_plasma(tmp_path, name="secops", events=None):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    ev = _write_events(tmp_path / "events.jsonl",
                       events or [WHIDS_HIT, TAP_MATCH, LOGON_FAIL,
                                  SCHTASK, INFO_EVENT])
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=f"plasma:{ev}", out=out,
                                 name=name, packages=None,
                                 extra_sources=None, join_on=None,
                                 since=None, header=None, secret_ref=None,
                                 secret_header=None))
    return out, ev


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res


# ── L1: the source connector ──────────────────────────────────────────────

def test_plasma_source_profiles_security_events(tmp_path):
    out, _ = _build_plasma(tmp_path)
    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "severity" in probes_src
    assert "event_type" in probes_src
    assert "whids_detect" in probes_src or "event_type" in probes_src


def test_plasma_instance_answers_security_questions(tmp_path):
    out, _ = _build_plasma(tmp_path)
    inst = load_instance(out)
    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert _res(env)["count"] == 5
    env = inst.ask("events by type", use_cache=False)
    assert env["probe"] == "events_by_type"
    types = {r["event_type"]: r["count"] for r in _res(env)["rows"]}
    assert types.get("whids_detect") == 1
    assert types.get("logon_fail") == 1


def test_plasma_bridge_reads_live_events(tmp_path):
    """The bridge re-reads events.jsonl at ask time — new events are
    visible without re-init."""
    out, ev_path = _build_plasma(tmp_path)
    with open(ev_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**LOGON_FAIL, "ts": "2026-11-04T10:00:00Z"})
                 + "\n")
    inst = load_instance(out)
    env = inst.ask("how many rows", use_cache=False)
    assert _res(env)["count"] == 6        # was 5, now 6


# ── L2: security-specific probes ──────────────────────────────────────────

def test_security_probes_scaffolded(tmp_path):
    out, _ = _build_plasma(tmp_path)
    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    for probe in ("critical_alerts", "failed_logons", "new_scheduled_tasks",
                  "events_by_type", "events_by_host"):
        assert f"def {probe}" in probes_src, f"missing probe: {probe}"


def test_critical_alerts_and_failed_logons_answer(tmp_path):
    out, _ = _build_plasma(tmp_path)
    inst = load_instance(out)
    env = inst.ask("critical alerts", use_cache=False)
    res = _res(env)
    assert res.get("count", 0) >= 1       # the WHIDS hit is critical
    env = inst.ask("failed logons", use_cache=False)
    res = _res(env)
    assert res.get("count", 0) >= 1       # the logon_fail


def test_security_probes_have_misuse_traps(tmp_path):
    """The v2.1.0 birth-trap contract: every security probe gets its
    misuse trap at first load, proven by the conforming router."""
    out, _ = _build_plasma(tmp_path)
    inst = load_instance(out)  # triggers birth-trap synthesis
    traps = json.loads(open(os.path.join(out, "traps.json"),
                            encoding="utf-8").read())
    qs = [t["q"] for t in traps["items"]]
    assert any("critical_alerts" in q for q in qs), \
        "critical_alerts must have its misuse trap"
    assert any("failed_logons" in q for q in qs)


# ── L3: the verified-answer loop on security data ─────────────────────────

def test_golden_bank_on_plasma_instance(tmp_path):
    out, _ = _build_plasma(tmp_path)
    from neuralosd.banks import record_golden
    record_golden(out, q="how many rows", expect_probe="row_count",
                  value=5, shape="count", by="secops", source="Plasma",
                  method="count")
    inst = load_instance(out)
    golden = json.loads(open(os.path.join(out, "golden.json"),
                             encoding="utf-8").read())
    summary = inst.golden_run(golden)
    assert summary["wrong"] == 0 and summary["answered_traps"] == 0


# ── L4: actuation with proof ──────────────────────────────────────────────

def test_actuation_dry_run_does_not_touch_the_system(tmp_path):
    from neuralosd.actuate import actuate
    calls = []
    report = actuate(
        action="block_ip", target="1.2.3.4", reason="C2 beacon",
        operator="analyst", instance_dir=str(tmp_path),
        side_effect=lambda: calls.append("BLOCKED"))
    assert report["dry_run"] is True
    assert report["applied"] is False
    assert calls == [], "dry-run must not fire the side effect"


def test_actuation_apply_fires_and_journals(tmp_path):
    from neuralosd.actuate import actuate
    calls = []
    dr = actuate(action="block_ip", target="1.2.3.4", reason="C2 beacon",
                 operator="analyst", instance_dir=str(tmp_path),
                 side_effect=lambda: calls.append("BLOCKED"))
    plan_id = dr["plan_id"]
    report = actuate(action="block_ip", target="1.2.3.4",
                     reason="C2 beacon", operator="analyst",
                     instance_dir=str(tmp_path),
                     side_effect=lambda: calls.append("BLOCKED"),
                     apply_plan=plan_id)
    assert report["applied"] is True
    assert calls == ["BLOCKED"]
    # journal has both entries:
    jl = os.path.join(str(tmp_path), "actuation_journal.jsonl")
    entries = [json.loads(l) for l in open(jl, encoding="utf-8")
               if l.strip()]
    assert len(entries) == 2
    assert entries[0]["dry_run"] is True
    assert entries[1]["applied"] is True
    assert entries[1]["plan_id"] == plan_id
    assert entries[1]["reason"] == "C2 beacon"


def test_actuation_requires_a_reason(tmp_path):
    from neuralosd.actuate import actuate
    with pytest.raises(SystemExit) as e:
        actuate(action="block_ip", target="1.2.3.4", reason="",
                operator="analyst", instance_dir=str(tmp_path))
    assert "reason" in str(e.value).lower()


def test_actuation_requires_a_plan_id_for_apply(tmp_path):
    from neuralosd.actuate import actuate
    with pytest.raises(SystemExit) as e:
        actuate(action="block_ip", target="1.2.3.4", reason="C2",
                operator="analyst", instance_dir=str(tmp_path),
                apply_plan=True)   # True but no plan id from a seen dry-run
    assert "dry-run" in str(e.value).lower() or "plan" in str(e.value).lower()


def test_actuation_unknown_action_is_refused(tmp_path):
    from neuralosd.actuate import actuate
    with pytest.raises(SystemExit) as e:
        actuate(action="nuke_everything", target="x", reason="y",
                operator="a", instance_dir=str(tmp_path))
    assert "unknown action" in str(e.value).lower()


def test_actuation_undo_journals_the_rollback(tmp_path):
    from neuralosd.actuate import actuate
    dr = actuate(action="block_ip", target="1.2.3.4", reason="C2",
                 operator="analyst", instance_dir=str(tmp_path))
    plan_id = dr["plan_id"]
    r = actuate(action="block_ip", target="1.2.3.4", reason="C2",
                operator="analyst", instance_dir=str(tmp_path),
                apply_plan=plan_id)
    undo = actuate(action="block_ip", target="1.2.3.4", reason="false positive",
                   operator="analyst", instance_dir=str(tmp_path),
                   undo_plan_id=plan_id)
    assert undo["undone"] is True
    jl = [json.loads(l) for l in
          open(os.path.join(str(tmp_path), "actuation_journal.jsonl"),
               encoding="utf-8") if l.strip()]
    assert jl[-1]["op"] == "undo"
    assert jl[-1]["plan_id"] == plan_id
