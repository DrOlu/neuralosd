"""Tests-first: v2.4.0 — verification depth, trust, fleet ops.

  C    value-level oracles for every answer shape: golden cases verify the
       ANSWER (typed: count / percent / ratio / date / text), not just the
       routing. Wrong value ≠ pass.
  D    snapshot pinning / time-travel: `neuralosd snapshot --label q3` freezes
       hashed observations; `ask --as-of q3` answers from the pin — even
       after the source changed.
  H    RBAC / row-level caging: acl.json gives roles probe allow-lists and
       row filters; load_instance(role=...) enforces both.
  I    refusal-rate SLO + fleet dashboard: slo computes the refusal rate over
       a window and exits 1 above SLO; fleet reports per-instance health.
  J    golden-bank CI badging: badge writes a shields.io endpoint JSON + a
       GitHub Actions snippet that gates on the bank.
  K    cluster-driven synonym generation: gap clusters become PROPOSED
       triggers for the probe that most plausibly owns them — human-approved,
       applied to derived metrics, emitted as suggestions for probes.py.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance


def _build(tmp_path, csv="region,units\nNorth,100\nSouth,200\nEast,50\nNorth,30\n",
           name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(csv, encoding="utf-8")
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


# ── C: value-level oracles ────────────────────────────────────────────────

def test_golden_verifies_the_answer_value_not_just_the_route(tmp_path):
    from neuralosd.banks import record_golden
    out = _build(tmp_path)
    record_golden(out, q="how many rows", expect_probe="row_count",
                  value=4, shape="count", by="olu", source="manual",
                  method="count")
    inst = load_instance(out)
    golden = json.loads(open(os.path.join(out, "golden.json"),
                             encoding="utf-8").read())
    summary = inst.golden_run(golden)
    assert summary["value_checked"] >= 1
    assert summary["wrong_values"] == 0
    assert summary["wrong"] == 0


def test_a_wrong_value_fails_even_when_routing_is_right(tmp_path):
    """THE test: the probe routes correctly and answers confidently — with
    the WRONG number. The typed oracle catches what routing-only banks miss."""
    from neuralosd.banks import record_golden
    out = _build(tmp_path)
    record_golden(out, q="how many rows", expect_probe="row_count",
                  value=999, shape="count", by="olu", source="manual",
                  method="count")
    inst = load_instance(out)
    golden = json.loads(open(os.path.join(out, "golden.json"),
                             encoding="utf-8").read())
    summary = inst.golden_run(golden)
    assert summary["wrong_values"] == 1
    assert summary["wrong"] == 1


def test_typed_invariants_per_shape():
    from neuralosd.oracles import check_answer
    assert check_answer("count", 214, 214) is True
    assert check_answer("count", 214, 214.0) is True
    assert check_answer("count", -1, -1) is False       # invariant: >= 0
    assert check_answer("percent", 26.6, 26.6) is True
    assert check_answer("percent", 126.6, 126.6) is False   # invariant: 0..100
    assert check_answer("date", "2026-11-02", "2026-11-02") is True
    assert check_answer("date", "2026-11-02", "02-11-2026") is False
    assert check_answer("text", "Canada", "canada ") is True   # casefold/space
    assert check_answer("text", "Canada", "Peru") is False


def test_answer_extraction_walks_the_results_tree(tmp_path):
    from neuralosd.oracles import extract_answer
    res = [{"rows": [{"region": "North", "units": 100},
                     {"region": "South", "units": 200}],
            "total": 300}]
    assert extract_answer(res, "total") == 300
    assert 300 in extract_answer(res, None)          # numeric leaf scan


# ── D: snapshot pinning / time-travel ─────────────────────────────────────

def _install_metric(out):
    from neuralosd.derived import (DerivedMetric, load as load_derived,
                                   save as save_derived)
    m = DerivedMetric(name="derived_units_per_row", kind="ratio",
                      numerator="total_units.sum",
                      denominator="row_count.count", scale="ratio",
                      description="units per row",
                      triggers=["units per row"],
                      question="units per row",
                      provenance={"model": "stub"})
    save_derived(os.path.join(out, "derived.json"),
                 load_derived(os.path.join(out, "derived.json")) + [m])
    return m


def test_pinned_snapshot_answers_time_travel_questions(tmp_path):
    from neuralosd.derived import load_snapshot
    from neuralosd.loop import pin_snapshot
    from neuralosd.router import NoResults
    out = _build(tmp_path)
    _install_metric(out)
    # give the loop a live snapshot so the metric routes at all:
    from neuralosd.menu_adapter import observe
    from neuralosd.derived import save_snapshot
    save_snapshot(out, observe(out),
                  __import__("neuralosd.derived", fromlist=["x"]).load(
                      os.path.join(out, "derived.json")))
    inst = load_instance(out)
    env = inst.ask("units per row", use_cache=False)
    live = _res(env)
    live_value = live.get("value", live.get("ratio"))

    pin = pin_snapshot(out, label="q3")
    assert pin["hash"]

    # THE SOURCE CHANGES UNDER THE INSTANCE, and the loop re-observes
    # (what `reason --refresh` does) — the live answer moves:
    bridge_src = open(os.path.join(out, "bridge.py"), encoding="utf-8").read()
    import re as _re
    m = _re.search(r'SOURCE = "([^"]+)"', bridge_src)
    with open(m.group(1), "w", encoding="utf-8") as fh:
        fh.write("region,units\nNorth,100\n")       # 1 row, 100 units
    from neuralosd.menu_adapter import observe
    from neuralosd.derived import save_snapshot, load as load_derived
    obs = observe(out)
    save_snapshot(out, obs, load_derived(os.path.join(out, "derived.json")))
    inst2 = load_instance(out)
    live2 = _res(inst2.ask("units per row", use_cache=False))
    live2_value = live2.get("value", live2.get("ratio"))

    # time travel: answer from the PIN, taken before the change:
    from neuralosd.loop import ask_as_of
    env_pinned = ask_as_of(out, "units per row", label="q3")
    pinned_value = _res(env_pinned).get("value", _res(env_pinned).get("ratio"))

    assert live2_value != live_value                # the drift is real
    assert pinned_value == live_value               # the pin froze it
    assert env_pinned["snapshot"]["label"] == "q3"
    assert env_pinned["snapshot"]["hash"] == pin["hash"]


# ── H: RBAC / row-level caging ────────────────────────────────────────────

def test_rbac_role_limits_probes_and_rows(tmp_path):
    out = _build(tmp_path)
    acl = {"roles": {
        "analyst": {"allow": ["row_count", "breakdown"],
                    "row_filter": {"column": "region", "in": ["North"]}}}}
    with open(os.path.join(out, "acl.json"), "w", encoding="utf-8") as fh:
        json.dump(acl, fh)

    inst = load_instance(out, role="analyst")
    names = [m.name for m in inst.router.metas]
    assert "list_rows" not in names                 # not allowed -> not routable
    assert "breakdown" in names

    env = inst.ask("units by region", use_cache=False)
    res = _res(env)
    rows = res.get("rows") or []
    assert rows and all(r["region"] == "North" for r in rows), \
        "row-level caging must filter the served rows"

    # no role = unaffected:
    inst_full = load_instance(out)
    env = inst_full.ask("units by region", use_cache=False)
    rows = _res(env).get("rows") or []
    assert {r["region"] for r in rows} == {"North", "South", "East"}


# ── I: refusal-rate SLO + fleet dashboard ─────────────────────────────────

def test_refusal_rate_computed_over_a_window(tmp_path):
    from neuralosd.loop import refusal_rate
    out = _build(tmp_path)
    now = time.time()
    rows = ([{"ts": now - 60, "question": f"bad q {i}", "probe": None,
              "results": None, "refusal_reason": "no_probe_matches"}
             for i in range(3)]
            + [{"ts": now - 60, "question": "how many rows",
                "probe": "row_count", "results": [{"count": 4}]}])
    with open(os.path.join(out, "ask_audit.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    # min_asks=4: this test is about the window arithmetic, not the E8
    # sample-size gate (a 4-ask window is undersized at the default 10).
    stats = refusal_rate(out, window_hours=24, min_asks=4)
    assert stats["asks"] == 4
    assert abs(stats["refusal_rate"] - 0.75) < 1e-9
    assert stats["insufficient_data"] is False
    assert stats["breach"] is True                  # default SLO 0.4


def test_fleet_dashboard_reports_each_instance(tmp_path):
    from neuralosd.loop import fleet_report
    a = _build(tmp_path, name="fa")
    b = _build(tmp_path, "region,units\nNorth,1\n", name="fb")
    rows = [{"ts": time.time(), "question": "x", "probe": None,
             "results": None, "refusal_reason": "no_probe_matches"}]
    for d in (a, b):
        with open(os.path.join(d, "ask_audit.jsonl"), "w",
                  encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    # One ask each: min_asks=1 so the fleet buckets, not the E8 gate, are
    # what this test checks.
    report = fleet_report([a, b], window_hours=24, max_refusal_rate=0.4,
                          min_asks=1)
    assert len(report["instances"]) == 2
    assert all(i["refusal_rate"] == 1.0 for i in report["instances"])
    assert report["breached"] == 2
    assert report["healthy"] == 0


# ── J: golden-bank CI badging ─────────────────────────────────────────────

def test_badge_reflects_bank_health(tmp_path):
    from neuralosd.banks import record_golden
    from neuralosd.loop import write_badge
    out = _build(tmp_path)
    record_golden(out, q="how many rows", expect_probe="row_count",
                  value=4, shape="count", by="olu", source="manual",
                  method="count")
    badge = write_badge(out, str(tmp_path / "badge.json"))
    assert badge["schemaVersion"] == 1
    assert badge["color"] == "brightgreen"
    assert "passing" in badge["message"]

    # break the bank with a wrong value:
    record_golden(out, q="total units", expect_probe="total_units",
                  value=999999, shape="count", by="olu", source="manual",
                  method="count")
    badge = write_badge(out, str(tmp_path / "badge.json"))
    assert badge["color"] == "red"


def test_badge_writes_a_ci_workflow_snippet(tmp_path):
    from neuralosd.loop import write_badge
    out = _build(tmp_path)
    wf = os.path.join(str(tmp_path), "golden.yml")
    write_badge(out, str(tmp_path / "badge.json"), actions_out=wf)
    text = open(wf, encoding="utf-8").read()
    assert "neuralosd golden" in text and "on:" in text


# ── K: cluster-driven synonym generation ──────────────────────────────────

def test_gap_clusters_propose_triggers_for_the_owning_probe(tmp_path):
    from neuralosd.loop import propose_synonyms
    out = _build(tmp_path)
    now = time.time()
    # near-miss refusals that consistently reach for the breakdown probe:
    rows = [{"ts": now, "question": q, "probe": None, "results": None,
             "refusal_reason": "below_floor", "scores": {"breakdown": 9}}
            for q in ["units by each region", "units per region",
                      "units across regions"]]
    with open(os.path.join(out, "ask_audit.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    proposal = propose_synonyms(out)
    assert proposal["proposals"], "a 3-strong cluster must propose something"
    p = proposal["proposals"][0]
    assert p["probe"] == "breakdown"
    toks = set(p["trigger"].split())
    # tokens pass the router's plural normalization ("units" -> "unit"):
    assert toks <= {"units", "unit", "region"}
    assert p["approved"] is False                   # human-approved, always


def test_apply_synonyms_extends_derived_metric_triggers_only_after_approval(
        tmp_path):
    from neuralosd.derived import load as load_derived, save as save_derived
    from neuralosd.loop import apply_synonyms, propose_synonyms
    out = _build(tmp_path)
    m = _install_metric(out)
    # a cluster that clearly belongs to the derived metric:
    now = time.time()
    rows = [{"ts": now, "question": q, "probe": None, "results": None,
             "refusal_reason": "below_floor", "scores": {m.name: 9}}
            for q in ["units per row now", "units per each row",
                      "units per single row"]]
    with open(os.path.join(out, "ask_audit.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    proposal = propose_synonyms(out)
    assert proposal["proposals"]
    proposal["proposals"][0]["approved"] = True     # the human says yes
    prop_path = os.path.join(out, "synonym_proposals.json")
    with open(prop_path, "w", encoding="utf-8") as fh:
        json.dump(proposal, fh)

    summary = apply_synonyms(out, prop_path)
    assert summary["applied"] >= 1
    stored = [m for m in load_derived(os.path.join(out, "derived.json"))
              if m.name == m.name]
    assert any("per" in t and "row" in t
               for mm in stored for t in mm.triggers), \
        "the approved trigger must be appended to the metric"
