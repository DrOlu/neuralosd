"""Regressions for the v2.0.4 field reports:

1. `neuralosd audit` flagged instance-local sibling modules (models.py,
   graph_bridge.py) as missing-everywhere — false positives, since the
   adapter puts the instance dir on sys.path and they always resolve.
2. Sidecar delegation never triggered for classic (menu-only) instances:
   the menu path skipped _wrap_with_sidecar_fallback entirely, and the
   sidecar itself refused instances without probes.py. A lazy import in a
   HELPER function (probe → helper → import) therefore failed with
   ModuleNotFoundError even with the sidecar provisioned and healthy.
"""

import json

import pytest

from neuralosd.instance_audit import (
    INSTANCE_LOCAL,
    audit_instance,
    classify_imports,
    collect_instance_imports,
    instance_local_modules,
)
from neuralosd.menu_adapter import load_menu_instance, menu_backed_probes
from neuralosd.sidecar import load_probe_functions


# ── bug 1: audit sibling-module false positives ─────────────────────────────
def test_sibling_modules_classified_instance_local(tmp_path):
    (tmp_path / "models.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "graph_bridge.py").write_text("Y = 2\n", encoding="utf-8")
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "bridge.py").write_text(
        "import models\n"
        "import graph_bridge\n"
        "import helpers.util\n"
        "import requests\n", encoding="utf-8")

    local = instance_local_modules(str(tmp_path))
    assert {"models", "graph_bridge", "helpers"} <= local

    classes = classify_imports(
        collect_instance_imports(str(tmp_path)),
        bundled={"requests"}, host=None,
        instance_local=local)
    assert classes["models"] == INSTANCE_LOCAL
    assert classes["graph_bridge"] == INSTANCE_LOCAL
    assert classes["helpers"] == INSTANCE_LOCAL
    assert classes["requests"] == "bundled"
    # zero missing-everywhere — the healthy instance must audit healthy
    assert not [m for m, c in classes.items() if c == "missing-everywhere"]


def test_audit_instance_ok_with_siblings(tmp_path):
    d = tmp_path / "dcir-portal-instance"
    d.mkdir()
    (d / "needle_menu.json").write_text(json.dumps(
        [{"name": "n", "description": "d", "triggers": ["t"],
          "parameters": {"type": "object", "properties": {}}}]), encoding="utf-8")
    (d / "models.py").write_text("CACHE = {}\n", encoding="utf-8")
    (d / "graph_bridge.py").write_text("import models\n", encoding="utf-8")
    (d / "bridge.py").write_text(
        "import graph_bridge\n\ndef n():\n    return graph_bridge.CACHE\n",
        encoding="utf-8")
    rep = audit_instance(str(d), bundled=set(), host=None)
    assert rep["ok"] is True, rep          # was False in v2.0.4
    assert rep["missing_everywhere"] == []


def test_sibling_shadowing_still_instance_local(tmp_path):
    """A sibling that SHADOWS a third-party name (models.py) wins: the
    instance dir is first on sys.path, so that is what the import binds to."""
    (tmp_path / "models.py").write_text("", encoding="utf-8")
    (tmp_path / "bridge.py").write_text("import models\n", encoding="utf-8")
    classes = classify_imports({"models"}, bundled=set(), host=None,
                               instance_local=instance_local_modules(str(tmp_path)))
    assert classes["models"] == INSTANCE_LOCAL


# ── bug 2: sidecar delegation for classic (menu-only) instances ─────────────
MENU = [{"name": "live_transactions_count", "description": "count",
         "triggers": ["how many transactions"],
         "parameters": {"type": "object", "properties": {}}}]

BRIDGE_SERVABLE = """\
def _sign_payload(payload):
    import sqlite3            # lazy, inside a HELPER (point-of-use style)
    con = sqlite3.connect(":memory:")
    n = con.execute("SELECT 1").fetchone()[0]
    con.close()
    return {"signed": n}


def live_transactions_count():
    return _sign_payload({})
"""


BRIDGE_LAZY_HELPER = """\
def _encrypt_login(payload):
    import no_such_module_sidecar_should_have    # lazy, in HELPER: repro shape
    return {"ok": True}


def live_transactions_count():
    return _encrypt_login({})
"""


@pytest.fixture
def menu_instance(tmp_path):
    d = tmp_path / "finacle-live-instance"
    d.mkdir()
    (d / "needle_menu.json").write_text(json.dumps(MENU), encoding="utf-8")
    (d / "bridge.py").write_text(BRIDGE_LAZY_HELPER, encoding="utf-8")
    return str(d)


def test_sidecar_serves_menu_only_instances(tmp_path):
    """The sidecar used to raise FileNotFoundError without probes.py — which
    made delegation impossible for classic-layout instances. It must now lift
    the menu onto bridge.py exactly like the in-binary adapter."""
    d = tmp_path / "healthy-instance"
    d.mkdir()
    (d / "needle_menu.json").write_text(json.dumps(MENU), encoding="utf-8")
    (d / "bridge.py").write_text(BRIDGE_SERVABLE, encoding="utf-8")
    probes = load_probe_functions(str(d))
    names = {p._probe.name for p in probes}
    assert names == {"live_transactions_count"}
    out = [p for p in probes if p._probe.name == "live_transactions_count"][0]
    assert out() == {"signed": 1}     # the lazy helper import resolved here


def test_lazy_helper_import_delegates_to_sidecar(menu_instance, monkeypatch):
    """The headline regression: probe → helper → import must delegate.

    The in-binary probe raises ModuleNotFoundError (no cryptography in the
    frozen runtime); the fallback wrapper must retry the probe via the
    sidecar, where cryptography EXISTS. Simulated with a fake client whose
    python has the module. Must go through _load_menu_instance — the
    production loader that attaches the fallback (the raw adapter does not)."""
    calls = []

    class FakeClient:
        def __init__(self, instance_dir):
            calls.append(instance_dir)

        def start(self):
            return self

        def call(self, probe, args):
            # stand-in for the sidecar python, which HAS the missing module:
            # it just answers, where the in-process probe raised
            return {"_tool": probe, "delegated": True}

    import neuralosd.sidecar_client as sc
    monkeypatch.setattr(sc, "sidecar_command", lambda: ["sidecar-exe"])
    monkeypatch.setattr(sc, "SidecarClient", FakeClient)

    from neuralosd._cmd._common import _load_menu_instance
    inst = _load_menu_instance(menu_instance)
    probe = [p for p in inst.probes
             if p._probe.name == "live_transactions_count"][0]
    result = probe()
    assert result.get("delegated") is True
    assert calls, "sidecar was never started"


def test_menu_entry_tier_sidecar_is_honored(tmp_path):
    """Option-3 escape hatch: a menu entry with "tier": "sidecar" must land
    in ProbeMeta.tier so _maybe_redirect_tier_sidecar can forward it — the
    adapter used to hardcode tier="in-process" for every lifted probe."""
    d = tmp_path / "inst"
    d.mkdir()
    (d / "needle_menu.json").write_text(json.dumps(
        [{"name": "heavy", "description": "d", "triggers": ["h"],
          "tier": "sidecar",
          "parameters": {"type": "object", "properties": {}}}]), encoding="utf-8")
    (d / "bridge.py").write_text("def heavy():\n    return {}\n", encoding="utf-8")
    probes = menu_backed_probes(str(d), [_bridge_mod(d)])
    meta = probes[0]._probe
    assert meta.tier == "sidecar"


def _bridge_mod(d):
    import importlib.util
    path = d / "bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_test_mod", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

