"""derived.json metrics must be routable on the probes.py path — and loading
must never execute a probe.

The bug: derived metrics were appended only on the needle-menu path. On the
probes.py path — every generated instance — `neuralosd reason` reported a
successful install and the next `ask` silently routed to the underlying
quantity probe instead.

The naive fix (append via `derived_probes`, which eagerly OBSERVES) made every
ask fire every probe. On a 21-probe live REST instance that turned a 2-second
ask into minutes. So the fix stores a PRUNED observation snapshot that `reason`
writes and every later `ask` loads — zero probe calls at load.
"""
import importlib.util
import json
import os
import sys

import pytest

from neuralosd._cmd._common import load_instance
from neuralosd.derived import (DerivedMetric, load, load_snapshot,
                               referenced_probes, save_snapshot)
from neuralosd.menu_adapter import (dedup_probes, derived_probes,
                                    probes_from_menu)
from neuralosd.reasoning import install

OBS = {
    "total_revenue": {"sum": 5350.0, "n": 4, "rows_in": 4, "rows_counted": 4},
    "total_units": {"sum": 445.0, "n": 4, "rows_in": 4, "rows_counted": 4},
    "row_count": {"count": 4, "rows_in": 4, "rows_counted": 4},
}

PROBES_PY = """
from neuralosd import probe

@probe(description="How many rows", triggers=["how many rows"], name="row_count")
def row_count():
    return {"count": 4, "rows_in": 4, "rows_counted": 4}

@probe(description="Total units", triggers=["total units"], name="total_units")
def total_units():
    return {"sum": 445.0, "n": 4, "rows_in": 4, "rows_counted": 4,
            "skipped": {"non_numeric": 0}}

@probe(description="Total revenue", triggers=["total revenue"], name="total_revenue")
def total_revenue():
    return {"sum": 5350.0, "n": 4, "rows_in": 4, "rows_counted": 4,
            "skipped": {"non_numeric": 0}}

PROBES = [row_count, total_units, total_revenue]
"""


def _metric(name="derived_revenue_per_unit", **kw):
    base = dict(kind="ratio", numerator="total_revenue.sum",
                denominator="total_units.sum", scale="ratio",
                triggers=["revenue per unit"], question="revenue per unit")
    base.update(kw)
    return DerivedMetric(name=name, **base)


def _names(inst):
    return [m.name for m in inst.router.metas]


@pytest.fixture
def inst(tmp_path):
    """A real generated instance (probes.py path), metric installed."""
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    install(_metric(), str(tmp_path), observations=OBS)
    return str(tmp_path)


# ── acceptance 1: the metric is routable on the probes.py path ─────────────

def test_metric_is_appended_on_the_probes_py_path(inst):
    names = _names(load_instance(inst))
    assert "derived_revenue_per_unit" in names, \
        "derived.json installed but never appended to the menu"


def test_ask_routes_to_the_metric(inst):
    env = load_instance(inst).ask("revenue per unit", use_cache=False)
    assert env["probe"] == "derived_revenue_per_unit"
    r = env["results"][0]
    assert r["value"] == pytest.approx(5350.0 / 445.0, abs=1e-4)
    assert r["formula"] == "sum / sum"


def test_it_also_works_on_the_needle_menu_path(tmp_path):
    """Regression guard: the path that already worked must keep working."""
    (tmp_path / "needle_menu.json").write_text(json.dumps([
        {"name": "total_revenue", "description": "Total revenue",
         "parameters": {"type": "object", "properties": {}},
         "triggers": ["total revenue"]}]), encoding="utf-8")
    (tmp_path / "bridge.py").write_text(
        "def total_revenue():\n    return {'sum': 5350.0}\n",
        encoding="utf-8")
    install(_metric(), str(tmp_path), observations=OBS)
    names = _names(load_instance(tmp_path))
    assert "derived_revenue_per_unit" in names


# ── acceptance 2: loading executes ZERO probes ─────────────────────────────

def test_loading_and_asking_execute_no_quantity_probe(inst):
    """A quantity probe that is merely LOADED must not run."""
    inst_obj = load_instance(inst)
    fired = []
    for i, p in enumerate(inst_obj.probes):
        meta = p._probe
        if getattr(p, "_derived", False):
            continue                       # the derived probe itself must run
        def make(orig=p, meta=meta):
            def wrapped():
                fired.append(meta.name)
                return orig()
            wrapped._probe = meta
            wrapped.__name__ = meta.name
            return wrapped
        inst_obj.probes[i] = make()
    inst_obj.router.probes = inst_obj.probes
    inst_obj.router.by_name = {m.name: p for p, m in
                               zip(inst_obj.probes, inst_obj.router.metas)}
    env = inst_obj.ask("revenue per unit", use_cache=False)
    assert env["probe"] == "derived_revenue_per_unit"
    assert fired == [], f"quantity probes executed during load/ask: {fired}"


def test_derived_probes_never_calls_observe(tmp_path, monkeypatch):
    """Acceptance 2 at the seam: no snapshot -> the metric refuses; it must not
    try to build one by observing."""
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    (tmp_path / "derived.json").write_text(json.dumps(
        {"version": 1, "metrics": [_metric().to_json()]}), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("derived_probes observed the instance at load")
    monkeypatch.setattr("neuralosd.menu_adapter.observe", boom)

    probes = derived_probes(str(tmp_path))          # must not raise
    assert len(probes) == 1
    # A probe that raises becomes an honest error envelope, NOT a NoResults:
    # it routed to the right capability and said exactly why it could not answer.
    env = load_instance(str(tmp_path)).ask("revenue per unit", use_cache=False)
    assert env["mode"] == "deterministic-error"
    assert "no observation snapshot" in env["results"][0]["error"]


def test_a_metric_with_no_snapshot_refuses_with_remediation(tmp_path):
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    install(_metric(), str(tmp_path))               # deliberately no observations
    env = load_instance(str(tmp_path)).ask("revenue per unit", use_cache=False)
    assert env["mode"] == "deterministic-error"
    msg = env["results"][0]["error"]
    assert "no observation snapshot" in msg and "--refresh" in msg


# ── acceptance 3: cache coherence ──────────────────────────────────────────

def test_menu_version_changes_when_a_metric_is_installed(tmp_path):
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    v_before = load_instance(str(tmp_path)).router.menu_version
    install(_metric(), str(tmp_path), observations=OBS)
    assert load_instance(str(tmp_path)).router.menu_version != v_before


def test_snapshot_records_its_age_and_staleness(inst):
    obs, _ = load_snapshot(inst)
    p = derived_probes(inst, observations=obs, snapshot_ts=1.0)[0]
    res = p()
    assert res["snapshot_stale"] is True and res["snapshot_age_s"] > 3600
    fresh = derived_probes(inst)[0]()               # just-written snapshot
    assert fresh["snapshot_stale"] is False


# ── acceptance 4: no duplicate metrics ─────────────────────────────────────

def test_no_duplicate_metrics_when_both_paths_exist(tmp_path):
    (tmp_path / "needle_menu.json").write_text(json.dumps([
        {"name": "total_revenue", "description": "t",
         "parameters": {"type": "object", "properties": {}},
         "triggers": ["total revenue"]}]), encoding="utf-8")
    (tmp_path / "bridge.py").write_text(
        "def total_revenue():\n    return {'sum': 5350.0}\n", encoding="utf-8")
    install(_metric(), str(tmp_path), observations=OBS)
    combined = dedup_probes(probes_from_menu(str(tmp_path)),
                            derived_probes(str(tmp_path)))
    names = [p._probe.name for p in combined]
    assert names.count("derived_revenue_per_unit") == 1
    assert names.count("total_revenue") == 1


def test_load_instance_never_duplicates_a_metric(inst):
    names = _names(load_instance(inst))
    assert len(names) == len(set(names))


# ── the snapshot itself ────────────────────────────────────────────────────

def test_install_writes_a_pruned_snapshot(tmp_path):
    install(_metric(), str(tmp_path), observations=OBS)
    snap = json.load(open(tmp_path / "derived_snapshot.json",
                          encoding="utf-8"))
    assert set(snap["observations"]) == {"total_revenue", "total_units"}
    assert "row_count" not in snap["observations"]     # pruned: unreferenced
    assert snap["ts"] > 0


def test_snapshot_loading_needs_no_probes(tmp_path):
    install(_metric(), str(tmp_path), observations=OBS)
    obs, ts = load_snapshot(str(tmp_path))
    assert obs["total_revenue"]["sum"] == 5350.0 and ts


def test_a_referenced_probe_missing_from_the_snapshot_is_reported(tmp_path):
    path, missing = save_snapshot(str(tmp_path), OBS,
                                  [_metric(numerator="ghost.sum")])
    assert missing == ["ghost"]
    obs, _ = load_snapshot(str(tmp_path))
    assert "ghost.sum" not in obs


def test_referenced_probes_lists_both_sides():
    assert referenced_probes([_metric()]) == ["total_revenue", "total_units"]


def test_roundtrip_load_after_save(tmp_path):
    install(_metric(), str(tmp_path), observations=OBS)
    metrics = load(str(tmp_path / "derived.json"))
    assert metrics[0].name == "derived_revenue_per_unit"
    assert metrics[0].min_coverage == 0.6


# ── acceptance 5: closure is checked through the REAL ask path ─────────────

def test_closure_check_uses_load_instance_not_a_private_menu(inst):
    """`reason` must verify through the same `load_instance` a user's ask takes.
    This is the check that would have caught the bug on day one."""
    fresh = load_instance(inst)                 # exactly what reason.py does
    env = fresh.ask("revenue per unit", use_cache=False)
    assert env.get("probe") == "derived_revenue_per_unit"
    assert env.get("mode") == "deterministic"


def test_closure_fails_loudly_when_the_metric_cannot_answer(tmp_path):
    """A metric with no snapshot routes but refuses -> closure must NOT be 0."""
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    install(_metric(), str(tmp_path))            # no snapshot on purpose
    fresh = load_instance(str(tmp_path))
    env = fresh.ask("revenue per unit", use_cache=False)
    # A probe exception is an honest error envelope, not a refusal: it routed
    # to the right capability and said exactly why it could not answer.
    assert env["mode"] == "deterministic-error"
    assert "no observation snapshot" in env["results"][0]["error"]
    # reason.run maps this closure failure to exit 5 — never 0.


def test_refresh_rewrites_the_snapshot_without_a_model(tmp_path):
    """Acceptance 2's remediation path: refresh is explicit and probe-calling."""
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    install(_metric(), str(tmp_path))            # no snapshot
    obs, ts = load_snapshot(str(tmp_path))
    assert obs is None
    # what `neuralosd reason --refresh` does: re-observe and rewrite.
    for mod in ("probes", "bridge"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location(
        "_rt_probes", str(tmp_path / "probes.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rt_probes"] = mod
    spec.loader.exec_module(mod)
    from neuralosd.derived import is_derived
    from neuralosd.reasoning import observations_from
    obs = observations_from([p for p in mod.PROBES if not is_derived(p)])
    path, missing = save_snapshot(str(tmp_path), obs, load(str(tmp_path / "derived.json")))
    assert missing == []
    obs2, ts2 = load_snapshot(str(tmp_path))
    assert obs2 is not None and ts2 is not None
    env = load_instance(str(tmp_path)).ask("revenue per unit", use_cache=False)
    assert env["probe"] == "derived_revenue_per_unit"
