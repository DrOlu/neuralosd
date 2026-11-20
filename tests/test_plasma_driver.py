"""Drive Plasma 1.24+ through its published `help --json` catalog.

A fake Plasma executable stands in for the real CLI: the tests pin catalog
validation, probe generation, routing, argument caging, the read-only
boundary, and the two-step plan/apply gate. No network, no Windows.
"""
import json
import os
import stat
import sys

import pytest

from neuralosd import plasma
from neuralosd._cmd import plasma as plasma_cli
from neuralosd._cmd._common import load_instance


CATALOG = {
    "schema": "plasma.help/1",
    "product": "Plasma",
    "version": "9.9.9",
    "commands": [
        {"command": "health", "path": ["health"], "group": "trust",
         "summary": "deterministic health verdict", "description": "d",
         "usage": "usage: plasma health", "leaf": True, "hidden": False,
         "safety": "read-only (no host changes)", "arguments": [],
         "examples": [{"command": "plasma health"}]},
        {"command": "coverage", "path": ["coverage"], "group": "detect",
         "summary": "report ATT&CK coverage and blind spots", "description": "d",
         "usage": "usage: plasma coverage", "leaf": True, "hidden": False,
         "safety": "read-only (no host changes)", "arguments": [],
         "examples": []},
        {"command": "control policy show",
         "path": ["control", "policy", "show"], "group": "trust",
         "summary": "print the effective agent policy", "description": "d",
         "usage": "usage: plasma control policy show", "leaf": True,
         "hidden": False, "safety": "read-only (no host changes)",
         "arguments": [], "examples": []},
        {"command": "detect test", "path": ["detect", "test"],
         "group": "detect", "summary": "run detection regression cases",
         "description": "d", "usage": "usage: plasma detect test",
         "leaf": True, "hidden": False,
         "safety": "read-only (no host changes)",
         "arguments": [
             {"dest": "limit", "option_strings": ["--limit"], "type": "int",
              "required": False, "positional": False, "default": "10"},
             {"dest": "root", "option_strings": ["--root"],
              "required": False, "positional": False}],
         "examples": []},
        {"command": "control policy set",
         "path": ["control", "policy", "set"], "group": "trust",
         "summary": "update the effective agent policy", "description": "d",
         "usage": "usage: plasma control policy set", "leaf": True,
         "hidden": False, "safety": "writes local Plasma state under --root",
         "arguments": [], "examples": []},
        {"command": "enforce pending", "path": ["enforce", "pending"],
         "group": "trust", "summary": "list containment decisions",
         "description": "d", "usage": "usage: plasma enforce pending",
         "leaf": True, "hidden": False,
         "safety": "read-only (no host changes)", "arguments": [],
         "examples": []},
        {"command": "enforce run", "path": ["enforce", "run"],
         "group": "trust", "summary": "evaluate findings", "description": "d",
         "usage": "usage: plasma enforce run", "leaf": True, "hidden": False,
         "safety": "plan-gated and reversible; requires --apply --plan",
         "arguments": [
             {"dest": "root", "option_strings": ["--root"],
              "required": False, "positional": False},
             {"dest": "apply", "option_strings": ["--apply"],
              "required": False, "positional": False},
             {"dest": "plan", "option_strings": ["--plan"], "type": "int",
              "required": False, "positional": False}],
         "examples": []},
        {"command": "prevent rollback", "path": ["prevent", "rollback"],
         "group": "prevent", "summary": "undo a prevention control",
         "description": "d", "usage": "usage: plasma prevent rollback",
         "leaf": True, "hidden": False,
         "safety": "plan-gated and reversible; requires --apply --plan",
         "arguments": [
             {"dest": "id", "option_strings": ["--id"], "type": "int",
              "required": True, "positional": False},
             {"dest": "root", "option_strings": ["--root"],
              "required": False, "positional": False}],
         "examples": []},
        {"command": "query", "path": ["query"], "group": "detect",
         "summary": "typed queries", "description": "d",
         "usage": "usage: plasma query SQL", "leaf": True, "hidden": False,
         "safety": "read-only (no host changes)",
         "arguments": [{"dest": "sql", "option_strings": [],
                        "required": True, "positional": True}],
         "examples": []},
    ],
}

FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_PLASMA_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\n")
if args[:2] == ["help", "--json"]:
    print(json.dumps({catalog}))
elif args[:1] == ["health"]:
    print(json.dumps({{"status": "ok", "checks": []}}))
elif args[:1] == ["coverage"]:
    print(json.dumps({{"coverage": 42}}))
elif args[:3] == ["control", "policy", "show"]:
    print(json.dumps({{"autonomy": "supervised"}}))
elif args[:2] == ["enforce", "pending"]:
    print(json.dumps({{"pending": []}}))
elif args[:2] == ["detect", "test"]:
    print(json.dumps({{"limit": args[args.index("--limit") + 1]}}))
elif args[:2] == ["enforce", "run"]:
    if "--apply" in args:
        print(json.dumps({{"applied": 1}}))
    else:
        print(json.dumps({{"plan": 7, "dry_run": 1}}))
elif args[:2] == ["prevent", "rollback"]:
    if "--apply" in args:
        print(json.dumps({{"rolled_back": args[args.index("--id") + 1]}}))
    else:
        print(json.dumps({{"plan": 9, "dry_run": 1}}))
else:
    sys.stderr.write("unknown command")
    sys.exit(2)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_PLASMA_LOG", str(log))
    bin_path = tmp_path / "plasma"
    bin_path.write_text(FAKE.format(python=sys.executable,
                                    catalog=repr(CATALOG)),
                        encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)
    return bin_path, log


def _argv_log(log):
    return [json.loads(line) for line in log.read_text(
        encoding="utf-8").splitlines()]


def test_catalog_validation(fake):
    bin_path, _ = fake
    cat = plasma.load_catalog(str(bin_path))
    assert cat["version"] == "9.9.9"
    index = plasma.catalog_index(cat)
    assert ("control", "policy", "show") in index
    assert plasma.safety_class(index[("health",)]) == "read_only"
    assert plasma.safety_class(index[("enforce", "run")]) == "plan_gated"
    assert plasma.safety_class(
        index[("control", "policy", "set")]) == "mutating"


def test_build_probes_exposes_only_generatable_read_only(fake):
    bin_path, _ = fake
    probes = plasma.build_probes(plasma.load_catalog(str(bin_path)),
                                 bin_path=str(bin_path))
    names = {p._probe.name for p in probes}
    assert "plasma_health" in names
    assert "plasma_control_policy_show" in names
    assert "plasma_enforce_pending" in names
    assert "plasma_detect_test" in names
    # plan-gated, mutating and required-positional commands are not probes
    assert "plasma_enforce_run" not in names
    assert "plasma_control_policy_set" not in names
    assert "plasma_query" not in names
    detect = next(p for p in probes if p._probe.name == "plasma_detect_test")
    assert detect._probe.args["limit"]["type"] == "integer"


def test_generated_instance_routes_and_calls_plasma(fake, tmp_path):
    bin_path, log = fake
    root = tmp_path / "root"
    cat = plasma.load_catalog(str(bin_path))
    inst_dir = tmp_path / "inst"
    plasma.write_probe_module(inst_dir, cat, root=str(root),
                              bin_path=str(bin_path))
    inst = load_instance(str(inst_dir))
    env = inst.ask("is the estate healthy")
    assert env["probe"] == "plasma_health"
    assert env["results"][0]["data"]["status"] == "ok"
    env = inst.ask("attack coverage")
    assert env["probe"] == "plasma_coverage"
    assert env["results"][0]["data"]["coverage"] == 42
    # typed argument + root injection
    env = inst.ask("detect test limit 25")
    assert env["results"][0]["data"]["limit"] == "25"
    calls = _argv_log(log)
    assert any(c[:2] == ["detect", "test"] and "--root" in c for c in calls)


def test_plasma_call_refuses_non_read_only(fake):
    bin_path, _ = fake
    cat = plasma.load_catalog(str(bin_path))
    idx = plasma.catalog_index(cat)
    with pytest.raises(plasma.PlasmaError, match="plan.gated|mutating"):
        plasma.plasma_call(idx[("control", "policy", "set")], {},
                           bin_path=str(bin_path))
    with pytest.raises(plasma.PlasmaError, match="plan.gated"):
        plasma.plasma_call(idx[("enforce", "run")], {},
                           bin_path=str(bin_path))


def test_plan_apply_is_two_step_and_one_shot(fake, tmp_path):
    bin_path, log = fake
    root = tmp_path / "root"
    cat = plasma.load_catalog(str(bin_path))
    idx = plasma.catalog_index(cat)
    row = plasma.plan_action(idx[("enforce", "run")], {},
                             bin_path=str(bin_path), root=str(root))
    assert row["plan_id"].startswith("plp_")
    assert row["plasma_plan_id"] == 7
    assert row["dry_run"]["dry_run"] == 1
    applied = plasma.apply_action(row["plan_id"], cat, bin_path=str(bin_path),
                                  root=str(root))
    assert applied["applied"]["result"]["applied"] == 1
    with pytest.raises(plasma.PlasmaError, match="already applied"):
        plasma.apply_action(row["plan_id"], cat, bin_path=str(bin_path),
                            root=str(root))
    apply_call = [c for c in _argv_log(log)
                  if c[:2] == ["enforce", "run"] and "--apply" in c]
    assert apply_call and "--plan" in apply_call[0]


def test_plan_requires_plan_gated_and_known_params(fake, tmp_path):
    bin_path, _ = fake
    cat = plasma.load_catalog(str(bin_path))
    idx = plasma.catalog_index(cat)
    with pytest.raises(plasma.PlasmaError, match="not plan-gated"):
        plasma.plan_action(idx[("health",)], {}, bin_path=str(bin_path),
                           root=str(tmp_path / "root"))
    with pytest.raises(plasma.PlasmaError, match="unknown option"):
        plasma.plan_action(idx[("prevent", "rollback")], {"bogus": 1},
                           bin_path=str(bin_path), root=str(tmp_path / "root"))
    with pytest.raises(plasma.PlasmaError, match="unknown option"):
        plasma.plasma_call(idx[("health",)], {"apply": True},
                           bin_path=str(bin_path))


def test_cli_init_call_and_plan(fake, tmp_path, capsys):
    bin_path, _ = fake
    inst_dir = tmp_path / "inst"

    class Init:
        plasma_cmd = "init"
        plasma_bin = str(bin_path)
        timeout = 10
        json = False
        root = str(tmp_path / "root")
        events = None
        out = str(inst_dir)
        group = None
        include = None

    assert plasma_cli.run(Init()) == 0
    assert (inst_dir / "probes.py").is_file()
    capsys.readouterr()

    class Call:
        plasma_cmd = "call"
        plasma_bin = str(bin_path)
        timeout = 10
        json = True
        root = str(tmp_path / "root")
        events = None
        plasma_command = ["health"]
        param = None

    assert plasma_cli.run(Call()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["status"] == "ok"

    class Plan:
        plasma_cmd = "plan"
        plasma_bin = str(bin_path)
        timeout = 10
        root = str(tmp_path / "root")
        events = None
        plasma_command = ["enforce run"]
        param = None

    assert plasma_cli.run(Plan()) == 0
    text = capsys.readouterr().out
    assert '"plan_id": "plp_' in text
