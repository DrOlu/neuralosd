"""E5-class drift, generalized: every CLI shim's getattr(a, ...) is a real dest.

E5 shipped because one shim read `a.apply_plan` while the parser set `plan`;
the test for actuate pinned that ONE file. This sweeps every command shim:

  * a standalone `getattr(a, "name", ...)` must name an argparse dest;
  * an `or` chain of reads (`getattr(a, "old", None) or getattr(a, "new",
    None)`) is ONE fallback group and resolves if ANY name is a real dest
    — the deliberate backwards-compatible shims (actuate, init) rely on it;
  * nested subparser dests count (mesh/door read flags owned by their
    sub-subparsers), so the sweep collects dests from the whole parser.

It found exactly one offender when written: `_cmd/gossip.py` read
`a.gossip_push` / `a.gossip_pull`, which no parser ever sets — the module
was dead code duplicating the inline mesh-gossip branches in cli.py. It was
removed rather than wired, so the CLI has one implementation.
"""
import ast
from pathlib import Path

import pytest

from neuralosd import cli

CMD_DIR = Path(__file__).resolve().parents[1] / "src" / "neuralosd" / "_cmd"


def _all_dests(parser) -> set:
    dests = set()

    def walk(p):
        for action in p._actions:
            if action.dest and action.dest != "==SUPPRESS==":
                dests.add(action.dest)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for sub in choices.values():
                    walk(sub)
    walk(parser)
    return dests


def _is_getattr_on_a(node) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name) and node.args[0].id == "a"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str))


def _read_groups(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    groups, chained = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            members = [v for v in node.values if _is_getattr_on_a(v)]
            if members:
                groups.append([m.args[1].value for m in members])
                chained.update(id(m) for m in members)
    for node in ast.walk(tree):
        if _is_getattr_on_a(node) and id(node) not in chained:
            groups.append([node.args[1].value])
    return groups


SHIMS = sorted(p for p in CMD_DIR.glob("*.py")
               if not p.name.startswith("__") and p.name != "_common.py")


@pytest.mark.parametrize("shim", SHIMS, ids=[p.name for p in SHIMS])
def test_every_shim_flag_is_a_parser_dest(shim):
    dests = _all_dests(cli.build_parser())
    groups = _read_groups(shim)
    unresolved = [g for g in groups if not any(n in dests for n in g)]
    assert not unresolved, (
        f"{shim.name}: read chain(s) {unresolved} resolve to no argparse "
        f"dest — the E5 class of silent dest mismatch")
