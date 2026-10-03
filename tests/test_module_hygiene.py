"""Module-level hygiene: catch silent no-ops before they cost a day.

`router.py` defined `extract_args` twice. The second shadowed the first, so the
fix for the `user 999` clamp was written into the FIRST copy and had no effect
on any live code path — the bug kept reproducing while the source clearly said
it was fixed. `_extract_one` was reachable only from the dead copy.

A duplicate definition is never intentional. Assert it can't happen again.
"""
import ast
import collections
import os

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "neuralosd")


# `skills/` holds standalone example scripts shipped as DATA (a user copies
# them out and runs them), not package modules. They import optional extras by
# design, so importing them here tests the CI image, not this code.
SKIP_DIRS = ("skills",)


def _test_files():
    root = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(root, f) for f in sorted(os.listdir(root))
            if f.endswith(".py")]


def _modules(include_data_scripts=False):
    for root, dirs, files in os.walk(SRC):
        rel = os.path.relpath(root, SRC)
        if not include_data_scripts and any(d in rel.split(os.sep)
                                            for d in SKIP_DIRS):
            dirs[:] = []
            continue
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _duplicates(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = []
    top = collections.Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top[node.name] += 1
    for name, n in top.items():
        if n > 1:
            found.append(f"{name} (top level, {n}x)")
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = collections.Counter()
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[sub.name] += 1
            for name, n in methods.items():
                if n > 1:
                    found.append(f"{node.name}.{name} ({n}x)")
    return found


@pytest.mark.parametrize("path", list(_modules()),
                         ids=lambda p: os.path.relpath(p, SRC))
def test_no_shadowed_definitions(path):
    dupes = _duplicates(path)
    assert not dupes, (
        f"{os.path.relpath(path, SRC)} defines the same name more than once: "
        f"{dupes}. The later definition wins, so edits to the earlier one are "
        f"silently discarded.")


def _encoding_offenders(path):
    """Calls that read/write text via the LOCALE default encoding.

    On macOS and Linux that is UTF-8, so it works. On Windows it is cp1252, and
    the first non-ASCII byte raises UnicodeDecodeError — which is why this class
    of bug survives a green CI on two platforms and fails on the third.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = getattr(f, "id", None) or getattr(f, "attr", None)
        has_enc = any(k.arg == "encoding" for k in node.keywords)
        if name in ("read_text", "write_text"):
            if not has_enc:
                bad.append(f"{name}() at line {node.lineno}")
        elif name == "open":
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if mode and "b" in str(mode):
                continue                      # binary is fine
            if not has_enc:
                bad.append(f"open() at line {node.lineno}")
    return bad


@pytest.mark.parametrize("path", list(_modules()) + list(_test_files()),
                         ids=lambda p: os.path.relpath(p, os.path.dirname(SRC)))
def test_text_io_declares_its_encoding(path):
    offenders = _encoding_offenders(path)
    assert not offenders, (
        f"{os.path.relpath(path, os.path.dirname(SRC))} uses the locale default "
        f"encoding: {offenders}. Pass encoding= explicitly; on Windows the "
        f"default is cp1252 and non-ASCII content raises UnicodeDecodeError.")


def test_router_has_exactly_one_extract_args():
    """The specific regression, pinned."""
    tree = ast.parse(open(os.path.join(SRC, "router.py"), encoding="utf-8").read())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert names.count("extract_args") == 1


def test_every_module_imports_cleanly():
    """A module that cannot be imported cannot be tested.

    A MISSING OPTIONAL EXTRA is expected (the base install has no boxlite, no
    needle, no pymysql) and is skipped. A SyntaxError, a circular import or a
    NameError is a real defect and fails. The line is ModuleNotFoundError vs
    everything else, so a genuine break cannot hide behind this skip.
    """
    import importlib
    skipped = []
    for path in _modules():
        m = os.path.relpath(path, os.path.dirname(SRC))[:-3].replace(os.sep, ".")
        if m.endswith(".__init__"):
            m = m[: -len(".__init__")]
        try:
            importlib.import_module(m)
        except ModuleNotFoundError as e:
            skipped.append(f"{m} (needs {e.name})")
    if skipped:
        print("skipped (optional extras not installed):")
        for s in skipped:
            print("  " + s)
