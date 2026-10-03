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


def _modules():
    for root, _dirs, files in os.walk(SRC):
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


def test_router_has_exactly_one_extract_args():
    """The specific regression, pinned."""
    tree = ast.parse(open(os.path.join(SRC, "router.py"), encoding="utf-8").read())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert names.count("extract_args") == 1


def test_every_module_imports_cleanly():
    """A module that cannot be imported cannot be tested."""
    import importlib
    mods = [os.path.relpath(p, os.path.dirname(SRC))[:-3].replace(os.sep, ".")
            for p in _modules()]
    for m in mods:
        if m.endswith(".__init__"):
            m = m[: -len(".__init__")]
        importlib.import_module(m)
