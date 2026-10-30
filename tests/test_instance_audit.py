"""Instance import audit — the generic fix for "frozen binary can't see
instance imports".

The class of failure: neuralosd's frozen binary loads instance
bridge.py/instance.py/probes.py dynamically, so the build cannot know their
imports. Bitten twice: stdlib _sqlite3 (fixed upstream in v2.0.2) and
third-party cryptography. These tests pin the DETECTION half so every
future instance is classified at birth instead of failing in production.
"""

import json
import os
import subprocess
import sys

import pytest

from neuralosd.instance_audit import (
    MISSING_EVERYWHERE,
    audit_instance,
    audit_root,
    classify_imports,
    collect_imports,
    collect_instance_imports,
    importable_set,
    instance_module_paths,
)


# ── collection (AST, not regex) ─────────────────────────────────────────────
def _write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src, encoding="utf-8")
    return str(p)


def test_collects_top_level_import(tmp_path):
    p = _write(tmp_path, "bridge.py", "import sqlite3\nimport json\n")
    assert collect_imports(p) == {"sqlite3", "json"}


def test_collects_from_import_and_dotted(tmp_path):
    p = _write(tmp_path, "bridge.py",
               "from datetime import datetime\n"
               "import cryptography.fernet\n"
               "from collections import OrderedDict\n")
    assert collect_imports(p) == {"datetime", "cryptography", "collections"}


def test_collects_function_level_imports(tmp_path):
    """Imports inside functions are invisible to build-time scanning but fail
    at probe time — the audit must see them too."""
    p = _write(tmp_path, "bridge.py",
               "def probe():\n"
               "    import openpyxl\n"
               "    def inner():\n"
               "        import requests\n"
               "        return requests\n"
               "    return openpyxl\n")
    assert collect_imports(p) == {"openpyxl", "requests"}


def test_collects_tryexcept_imports(tmp_path):
    """Optional-import fallbacks still declare the dependency."""
    p = _write(tmp_path, "bridge.py",
               "try:\n"
               "    import ujson as json\n"
               "except ImportError:\n"
               "    import json\n")
    assert collect_imports(p) == {"ujson", "json"}


def test_relative_imports_skipped(tmp_path):
    """Relative imports resolve inside the instance dir, not the runtime."""
    p = _write(tmp_path, "bridge.py", "from . import helpers\nfrom .x import y\n")
    assert collect_imports(p) == set()


def test_instance_internal_crossimports_not_flagged(tmp_path):
    """probes.py doing `import bridge` is instance-local (the adapter puts
    the dir on sys.path) — it must never count as a runtime gap."""
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text(
        "import bridge\nfrom neuralosd import probe\n", encoding="utf-8")
    (d / "bridge.py").write_text("import sqlite3\n", encoding="utf-8")
    mods = collect_instance_imports(str(d))
    assert "bridge" not in mods        # instance-local, filtered
    assert "probes" not in mods
    assert mods == {"sqlite3", "neuralosd"}


def test_aliases_collected_once(tmp_path):
    p = _write(tmp_path, "bridge.py",
               "import sqlite3 as s3\nimport sqlite3\n")
    assert collect_imports(p) == {"sqlite3"}


# ── classification ──────────────────────────────────────────────────────────
def test_classification_three_classes():
    classes = classify_imports(
        ["sqlite3", "cryptography", "doesnotexist_anywhere"],
        bundled={"sqlite3"},          # what the frozen payload carries
        host={"sqlite3", "cryptography"})  # what the sidecar carries
    assert classes["sqlite3"] == "bundled"
    assert classes["cryptography"] == "host-only"
    assert classes["doesnotexist_anywhere"] == MISSING_EVERYWHERE


def test_unknown_host_never_passes_silently(tmp_path):
    """With no sidecar python to probe, unproven modules must be reported
    missing-everywhere — the audit refuses to guess in the instance's favor."""
    classes = classify_imports(["cryptography"], bundled=set(), host=None)
    assert classes["cryptography"] == MISSING_EVERYWHERE


def test_stdlib_without_bundled_set_classified_host_only():
    """Genuine stdlib with an unknown host: the binary ships most of the
    stdlib subset it was built with, so flag for the empirical check rather
    than declare failure — but never claim 'bundled' without proof."""
    classes = classify_imports(["csv"], bundled=set(), host=None)
    assert classes["csv"] == "host-only"


# ── instance-level audit ────────────────────────────────────────────────────
@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "finacle-users-instance"
    d.mkdir()
    (d / "needle_menu.json").write_text(json.dumps(
        [{"name": "finacle_users_count", "description": "count",
          "triggers": ["count finacle users"],
          "parameters": {"type": "object", "properties": {}}}]), encoding="utf-8")
    (d / "bridge.py").write_text(
        "import sqlite3\n"
        "from cryptography.fernet import Fernet\n"
        "\n"
        "def finacle_users_count():\n"
        "    return {'n': 1}\n", encoding="utf-8")
    return str(d)


def test_instance_module_paths(instance_dir):
    names = [os.path.basename(p) for p in instance_module_paths(instance_dir)]
    assert names == ["bridge.py"]          # instance.py/probes.py absent


def test_audit_instance_reports_the_dcir_class(instance_dir):
    """The exact production shape: sqlite3 bundled, cryptography host-only,
    nothing missing — plus the failure case when the sidecar lacks crypto."""
    bundled = {"sqlite3"}
    host = {"sqlite3", "cryptography"}
    rep = audit_instance(instance_dir, bundled, host)
    assert rep["ok"] is True
    assert rep["imports"]["sqlite3"] == "bundled"
    assert rep["imports"]["cryptography"] == "host-only"

    broken = audit_instance(instance_dir, bundled={"sqlite3"}, host={"sqlite3"})
    assert broken["ok"] is False
    assert broken["missing_everywhere"] == ["cryptography"]


def test_audit_root_finds_all_instances(tmp_path):
    """Fleet scan: every dir with needle_menu.json under a root is audited —
    the watchdog folder and task workdirs in one pass."""
    for name in ("a-instance", "nested/b-instance"):
        d = tmp_path / name
        d.mkdir(parents=True)
        (d / "needle_menu.json").write_text("[]", encoding="utf-8")
        (d / "bridge.py").write_text("import sqlite3\n", encoding="utf-8")
    (tmp_path / "not-an-instance").mkdir()
    (tmp_path / "not-an-instance" / "bridge.py").write_text(
        "import os\n", encoding="utf-8")
    report = audit_root(str(tmp_path), host_exe=sys.executable)
    found = sorted(r["instance"] for r in report["instances"])
    assert found == ["a-instance", "b-instance"]
    assert report["ok"] is True


# ── empirical probing ───────────────────────────────────────────────────────
def test_importable_set_empirical():
    """The probe asks a real python what it can import — no assumptions."""
    ok = importable_set(sys.executable, ["sqlite3", "no_such_module_xyz"])
    assert "sqlite3" in ok
    assert "no_such_module_xyz" not in ok


def test_importable_set_bad_exe_returns_empty():
    assert importable_set("/no/such/python", ["sqlite3"]) == set()


# ── end-to-end through the CLI ──────────────────────────────────────────────
def test_cli_audit_exit_codes(tmp_path, capsys):
    """Exit 0 when satisfied, non-zero when the instance would fail — this is
    the gate the instance-creation flow hangs on."""
    from neuralosd._cmd import audit as audit_cmd

    good = _write(tmp_path, "bridge.py",
                  "import sqlite3\n\ndef c():\n    return {'n': 1}\n")
    (tmp_path / "needle_menu.json").write_text(
        json.dumps([{"name": "c", "description": "count", "triggers": ["count"],
                     "parameters": {"type": "object", "properties": {}}}]),
        encoding="utf-8")

    class A:
        instance_dir = str(tmp_path)
        root = None
        host_python = sys.executable
        json = False

    audit_cmd.run(A())                      # sqlite3 is importable → exits 0
    out = capsys.readouterr().out
    assert "bundled" in out or "host-only" in out

    (tmp_path / "bridge.py").write_text(
        "import no_such_module_qqq\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        audit_cmd.run(A())
    assert e.value.code == 1
