"""FP / FN / edge-case sweep across runtime surfaces.

Written as an adversarial pass over the modules: each test encodes an edge
case we expect a careful implementation to handle. Failures here are real
bugs (found: header-path query-string auth bypass for /healthz, /ready
executing a real probe without a token, audit crashing on unparseable or
non-UTF8 bridge files, directories named "*.py" counted as modules, and the
SDK client crashing on non-JSON 200 responses).
"""

import json

import pytest

from neuralosd.instance_audit import (
    audit_instance,
    classify_imports,
    collect_imports,
    instance_local_modules,
)


# ── audit: unparseable and non-UTF8 files must not crash the auditor ───────
def test_audit_survives_syntax_error_bridge(tmp_path):
    """A bridge with a SyntaxError cannot load at runtime — the audit must
    REPORT it, not crash with a traceback."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / "needle_menu.json").write_text("[]", encoding="utf-8")
    (d / "bridge.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    rep = audit_instance(str(d), bundled=set(), host=set())
    assert rep["ok"] is False
    assert len(rep["parse_errors"]) == 1
    assert rep["parse_errors"][0].startswith("bridge.py:")


def test_audit_survives_non_utf8_bridge(tmp_path):
    """Windows-authored bridges can be cp1252 — read robustly, flag the file."""
    d = tmp_path / "cp1252"
    d.mkdir()
    (d / "needle_menu.json").write_text("[]", encoding="utf-8")
    (d / "bridge.py").write_bytes(b"x = '\xff\xfe invalid utf8'\n")
    rep = audit_instance(str(d), bundled=set(), host=set())
    assert rep["ok"] is False
    assert len(rep["parse_errors"]) == 1
    assert rep["parse_errors"][0].startswith("bridge.py:")


def test_collect_imports_swallows_syntax_errors_cleanly(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("def (:\n", encoding="utf-8")
    assert collect_imports(str(p)) == set()      # empty, no exception


# ── audit: directory-name edge cases ────────────────────────────────────────
def test_dir_named_like_py_file_is_not_a_module(tmp_path):
    (tmp_path / "fake.py").mkdir()               # a DIRECTORY named fake.py
    (tmp_path / "fake.py" / "keep.txt").write_text("", encoding="utf-8")
    (tmp_path / "real.py").write_text("", encoding="utf-8")
    local = instance_local_modules(str(tmp_path))
    assert "real" in local
    assert "fake" not in local                   # not importable as a module


def test_hidden_and_dunder_files_ignored(tmp_path):
    (tmp_path / ".hidden.py").write_text("", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    assert instance_local_modules(str(tmp_path)) == set()


def test_future_import_ignored_in_classification():
    """`from __future__ import annotations` must not count as a runtime gap."""
    classes = classify_imports(["__future__"], bundled=set(), host=set())
    assert classes["__future__"] != "missing-everywhere"



# ── service auth: path normalization + /ready data exposure ────────────────
# covered end-to-end in test_serve_auth.py additions below (real server).


# ── sdk client: hostile responses ───────────────────────────────────────────
# covered in test_sdk_client.py additions below.
