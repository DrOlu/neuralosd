"""Tests-first: loop improvements v2.3.0 — lifecycle features.

  F4  schema-similarity probe transfer: a probe proven on instance A is
      PROPOSED for schema-similar instance B, golden bank translated and
      flagged re-oracle. A proposal — never an install.
  F6  shadow mode: reason-installed probes are not routable while shadowed;
      they are evaluated against their oracle and promoted only on agreement.
  F8  auto-deprecation: probes nobody queried in N days are mothballed
      (kept, demoted from the menu) and can be reinstated.
  F9  schema-drift re-audit: when the source schema changes, probes whose
      caged enums / name-caged columns no longer exist are flagged BEFORE
      they fail at runtime.
  F10 federated gap fingerprints: peers share anonymized gap fingerprints
      (hashes of domain-token patterns + counts) — never the questions.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance


def _build(tmp_path, csv, name):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / f"{name}.csv"
    src.write_text(csv, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None))
    return out


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res


# ── F4: schema-similarity probe transfer ───────────────────────────────────

def test_transfer_proposes_probes_for_a_schema_sibling(tmp_path):
    """Same schema shape, different estate: A's breakdown probe + its golden
    items are PROPOSED for B (translated, re-oracle flagged). A proposal —
    B's own files are untouched except the proposal document."""
    from neuralosd.loop import propose_transfer
    a = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "estate_a")
    b = _build(tmp_path, "region,units\nEast,50\nWest,25\n", "estate_b")
    # A has a verified golden item for its breakdown probe:
    golden_path = os.path.join(a, "golden.json")
    golden = json.loads(open(golden_path, encoding="utf-8").read())
    golden["items"].append({"q": "units by region",
                            "expect_probe": "breakdown",
                            "oracle": {"value": 300, "by": "olu"}})
    open(golden_path, "w", encoding="utf-8").write(
        json.dumps(golden, indent=2))

    # A also carries a derived metric B does not have — the PROPOSED path:
    from neuralosd.derived import DerivedMetric, load as load_derived, \
        save as save_derived
    m = DerivedMetric(name="derived_north_units", kind="ratio",
                      numerator="total_units.sum",
                      denominator="row_count.count", scale="ratio",
                      description="north units per row",
                      triggers=["north units share"],
                      question="north units share",
                      provenance={"model": "stub"})
    metrics = load_derived(os.path.join(a, "derived.json"))
    save_derived(os.path.join(a, "derived.json"), metrics + [m])

    proposal = propose_transfer(a, b, min_sim=0.5)

    assert proposal["similarity"] >= 0.5
    # PROPOSED: A has it, B lacks it:
    names = [p["probe"] for p in proposal["probes"]]
    assert "derived_north_units" in names
    # RECONFIRMED: both have it, and A's verified golden translates with a
    # re-oracle flag — the value on B must be re-verified, not assumed:
    assert "breakdown" in proposal["reconfirmed"]
    g = [x for x in proposal["reconfirmed"]["breakdown"]["golden"]
         if x["q"] == "units by region"]
    assert g and g[0]["reoracle"] is True
    assert g[0]["provenance"]["source"] == "transfer:estate_a"


def test_transfer_refuses_dissimilar_schemas(tmp_path):
    from neuralosd.loop import propose_transfer
    a = _build(tmp_path, "region,units\nNorth,100\n", "finacle_a")
    b = _build(tmp_path, "host,cpu_pct\nweb1,55\nweb2,71\n", "monitor_b")
    proposal = propose_transfer(a, b, min_sim=0.9)
    assert proposal["similarity"] < 0.9
    assert proposal["probes"] == []


def test_transfer_never_writes_to_the_target_instance(tmp_path):
    from neuralosd.loop import propose_transfer
    a = _build(tmp_path, "region,units\nNorth,100\nSouth,1\n", "ta")
    b = _build(tmp_path, "region,units\nEast,50\nWest,2\n", "tb")
    before = open(os.path.join(b, "probes.py"), encoding="utf-8").read()
    propose_transfer(a, b, min_sim=0.5)
    assert open(os.path.join(b, "probes.py"),
                encoding="utf-8").read() == before
    assert not os.path.exists(os.path.join(b, "golden.json")) or \
        json.loads(open(os.path.join(b, "golden.json"),
                        encoding="utf-8").read())["items"] == \
        json.loads(open(os.path.join(b, "golden.json"),
                        encoding="utf-8").read())["items"]


# ── F6: shadow mode for generated probes ───────────────────────────────────

def _shadow_metric(name="derived_units_share", trigger="units share",
                   oracle=None):
    from neuralosd.derived import DerivedMetric
    return DerivedMetric(
        name=name, kind="ratio",
        numerator="total_units.sum", denominator="row_count.count",
        scale="ratio", description="units per row", triggers=[trigger],
        question="what is the units share",
        provenance={"model": "stub", "verified_against": oracle})


def _true_ratio(out, metric):
    """The value the loop will actually compute on fresh observations."""
    from neuralosd.derived import evaluate
    from neuralosd.menu_adapter import observe
    return evaluate(metric, observe(out))["value"]


def test_a_shadowed_probe_is_not_routable(tmp_path):
    from neuralosd.derived import load as load_derived, save as save_derived
    from neuralosd.loop import shadow_install, shadow_report
    out = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "sh")
    metric = _shadow_metric(oracle=150.0)        # 300 units / 2 rows
    assert _true_ratio(out, metric) == 150.0
    save_derived(os.path.join(out, "derived.json"), [metric])
    shadow_install(metric, out, days=14)

    inst = load_instance(out)
    from neuralosd.router import NoResults
    try:
        env = inst.ask("units share", use_cache=False)
    except NoResults as e:
        env = getattr(e, "envelope", {}) or {}
    assert env.get("probe") != metric.name, \
        "a shadowed probe must not serve traffic"
    # ...but it IS registered, and the report says so:
    report = shadow_report(out)
    assert report["shadowed"] == [metric.name]


def test_shadow_promotes_only_on_agreement(tmp_path):
    from neuralosd.derived import (load as load_derived, save as save_derived)
    from neuralosd.loop import shadow_install, shadow_report, shadow_promote
    out = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "sh2")
    metric = _shadow_metric(oracle=150.0)
    save_derived(os.path.join(out, "derived.json"), [metric])
    shadow_install(metric, out, days=0)

    # too young / too few runs → promote refused:
    assert shadow_promote(out, metric.name, min_agreement=0.9,
                          min_runs=3, max_age_days=0) is False
    # evaluate against the oracle three times — the metric agrees with
    # itself by construction (100/100), so agreement accrues:
    from neuralosd.loop import shadow_evaluate
    for _ in range(3):
        r = shadow_evaluate(out, metric.name)
        assert r["agreed"] is True
    assert shadow_promote(out, metric.name, min_agreement=0.9,
                          min_runs=3, max_age_days=0) is True

    inst = load_instance(out)
    env = inst.ask("units share", use_cache=False)
    assert env.get("probe") == metric.name      # now routable


def test_shadow_flags_disagreement_with_the_oracle(tmp_path):
    """A generated probe that drifts from its oracle accrues DISAGREEMENT —
    the 'confidently wrong generated probe' class, caught in the shadow."""
    from neuralosd.derived import load as load_derived, save as save_derived
    from neuralosd.loop import shadow_install, shadow_evaluate
    out = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "sh3")
    metric = _shadow_metric(oracle=41.0)             # the oracle says 41
    assert _true_ratio(out, metric) != 41.0
    save_derived(os.path.join(out, "derived.json"), [metric])
    shadow_install(metric, out, days=0)

    r = shadow_evaluate(out, metric.name)
    assert r["agreed"] is False
    stored = load_derived(os.path.join(out, "derived.json"))[0]
    assert stored.shadow["agreement"][-1]["agreed"] is False


# ── F8: auto-deprecation / menu hygiene ────────────────────────────────────

def test_stale_probes_are_mothballed_and_reinstatable(tmp_path):
    from neuralosd.loop import mothball, mothballed_names, reinstate
    out = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "mb")
    # audit: breakdown queried yesterday; row_count last queried a year ago
    now = time.time()
    audit = "\n".join(json.dumps(r) for r in [
        {"ts": now - 86400, "question": "units by region",
         "probe": "breakdown", "results": [{"by": "region"}]},
        {"ts": now - 365 * 86400, "question": "how many rows",
         "probe": "row_count", "results": [{"count": 2}]},
    ]) + "\n"
    open(os.path.join(out, "ask_audit.jsonl"), "w",
         encoding="utf-8").write(audit)

    report = mothball(out, age_days=180, apply=True)
    assert "row_count" in report["mothballed"]
    assert "breakdown" not in report["mothballed"]
    assert "row_count" in mothballed_names(out)

    inst = load_instance(out)
    assert "row_count" not in [m["name"] for m in inst.menu]
    assert "breakdown" in [m["name"] for m in inst.menu]

    reinstate(out, "row_count")
    inst = load_instance(out)
    assert "row_count" in [m["name"] for m in inst.menu]


def test_mothball_refuses_to_empty_the_menu(tmp_path):
    from neuralosd.loop import mothball, mothballed_names
    out = _build(tmp_path, "region,units\nNorth,100\n", "mb2")
    report = mothball(out, age_days=0, apply=True)   # everything is stale
    assert len(report["mothballed"]) < len(report["candidates"]), \
        "mothballing every probe would leave the instance mute"
    # ...and at least one probe still routes:
    inst = load_instance(out)
    assert inst.menu


# ── F9: schema-drift re-audit ──────────────────────────────────────────────

def test_column_removal_flags_the_probes_that_cage_it(tmp_path):
    from neuralosd.loop import audit_drift
    out = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "dr")
    new_src = tmp_path / "new.csv"
    new_src.write_text("region\nNorth\nSouth\n", encoding="utf-8")

    report = audit_drift(out, str(new_src))

    stale = {s["probe"] for s in report["stale_probes"]}
    assert "total_units" in stale               # the column is GONE
    assert "average_units" in stale
    assert "breakdown" in stale                 # its measure cage includes
    # the dead column — the default would fail at runtime
    assert "row_count" not in stale and "distinct_region" not in stale
    assert "units" in report["missing_columns"]


def test_enum_cage_vs_new_values_is_flagged(tmp_path):
    from neuralosd.loop import audit_drift
    out = _build(tmp_path, "region,units\nNorth,100\nSouth,200\n", "dr2")
    # region column now holds onlyNorthern-hemisphere values; a caged enum
    # (built at generation time) can no longer be filled for East/West.
    new_src = tmp_path / "new2.csv"
    new_src.write_text("region,units\nNorth,1\nSouth,2\n", encoding="utf-8")

    report = audit_drift(out, str(new_src))
    assert report["source_kind"] == "delimited"
    assert report["columns_now"]["region"] is not None


# ── F10: federated gap fingerprints ────────────────────────────────────────

AUDIT_A = "\n".join(json.dumps(r) for r in [
    {"ts": 1, "question": "shipping cost per unit", "probe": None,
     "results": None, "refusal_reason": "no_probe_matches"},
    {"ts": 2, "question": "what is the shipping cost for each unit",
     "probe": None, "results": None, "refusal_reason": "no_probe_matches"},
])
AUDIT_B = "\n".join(json.dumps(r) for r in [
    {"ts": 1, "question": "cost of shipping per unit shipped", "probe": None,
     "results": None, "refusal_reason": "no_probe_matches"},
    {"ts": 2, "question": "refund policy", "probe": None,
     "results": None, "refusal_reason": "no_probe_matches"},
])


def _gaps_out(tmp_path, audit, tag):
    from neuralosd._cmd import gaps as gaps_cmd
    log_path = str(tmp_path / f"audit_{tag}.jsonl")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(audit)

    class A:
        log = log_path
        menu = ""
        out = str(tmp_path / f"gaps_{tag}.json")
        export_fingerprints = str(tmp_path / f"fp_{tag}.json")
        ingest_fingerprints = None

    gaps_cmd.run(A)
    return json.loads(open(A.out, encoding="utf-8").read())


def test_fingerprints_export_counts_without_questions(tmp_path):
    out = _gaps_out(tmp_path, AUDIT_A, "a")
    fp = json.loads(open(os.path.join(tmp_path, "fp_a.json"),
                         encoding="utf-8").read())
    blob = json.dumps(fp)
    assert "shipping" not in blob.lower()       # NEVER the questions
    assert "per unit" not in blob.lower()
    assert fp["version"] == 1
    assert all(f["count"] >= 1 for f in fp["fingerprints"])
    assert out["count"] == 2


def test_ingesting_a_peer_fingerprint_boosts_the_local_gap(tmp_path):
    from neuralosd._cmd import gaps as gaps_cmd
    _gaps_out(tmp_path, AUDIT_A, "a")
    _gaps_out(tmp_path, AUDIT_B, "b")          # peer B exports its own

    class A2:
        log = str(tmp_path / "audit_a.jsonl")
        menu = ""
        out = str(tmp_path / "gaps_fed.json")
        export_fingerprints = str(tmp_path / "fp_fed.json")
        ingest_fingerprints = str(tmp_path / "fp_b.json")

    gaps_cmd.run(A2)
    fed = json.loads(open(A2.out, encoding="utf-8").read())
    # the shipping-cost pattern exists on BOTH edges: the cluster's mesh
    # count must reflect the peer, expressed in cost framing.
    mesh = [c for c in fed["clusters"] if c.get("mesh_count")]
    assert any(c["mesh_count"] > c["size"] for c in mesh), \
        "the peer saw the same pattern — the mesh count must say so"
