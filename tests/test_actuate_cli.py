"""E5 + E6 — the actuate CLI was unable to apply anything, and lied when
it (supposedly) did.

E5 (P0): cli.py registers --plan (argparse dest "plan") and --undo-plan
(dest "undo_plan"), but the shim read a.apply_plan / a.undo_plan_id —
attributes that never exist. --apply --plan <id> therefore always passed
apply_plan=None into actuate(), which took the DRY-RUN branch: every CLI
attempt minted a fresh plan id, nothing was ever applied, and both the
one-shot guard (E4) and the blind-actuation guard were unreachable from
the only documented entry point.

E6 (P1): the shim wires side_effect = lambda: None (a stub), then printed
"✓ APPLIED and journaled" and journaled applied: true — telling the
operator and every later journal reader that a host change happened when
nothing did. The executor is now honestly stamped: "stub": true in every
journal entry written through the CLI, and the success notice says so.

These tests drive the real CLI in a subprocess (the CLI is a new process
every invocation — that is the whole point of the journal-based guards)
and assert on stdout/stderr/exit code plus the journal file on disk.
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

INST = None  # set per-test via fixture


def _run(inst, *args):
    return subprocess.run(
        [sys.executable, "-m", "neuralosd", "actuate",
         "--instance-dir", str(inst)] + list(args),
        capture_output=True, text=True, timeout=120)


def _report(proc):
    """Parse the leading JSON object of a CLI report — the human notice
    that follows it is not part of the machine envelope."""
    obj, _ = json.JSONDecoder().raw_decode(proc.stdout)
    return obj


def _ops(inst):
    path = Path(inst) / "actuation_journal.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


ACT = ("--action", "block_ip", "--target", "203.0.113.9",
       "--reason", "C2 beacon", "--operator", "qa")


class TestE5CliCanApply:
    def test_apply_without_plan_exits_nonzero(self, tmp_path):
        # Blind actuation is refused with a message that names --plan.
        # (Pre-fix this silently dry-ran and exited 0.)
        r = _run(tmp_path / "inst", *ACT, "--apply")
        assert r.returncode != 0
        assert "--plan" in r.stderr
        assert all(e.get("op") != "apply" for e in _ops(tmp_path / "inst"))

    def test_dry_run_then_apply_journals_apply(self, tmp_path):
        inst = tmp_path / "inst"
        r1 = _run(inst, *ACT)  # no flags = dry-run
        assert r1.returncode == 0
        plan = _report(r1)["plan_id"]
        assert _report(r1)["dry_run"] is True

        r2 = _run(inst, *ACT, "--apply", "--plan", plan)
        assert r2.returncode == 0, r2.stderr
        assert _report(r2)["applied"] is True
        # E5: the plan id is CONSUMED, not re-minted — the applied entry
        # carries the dry-run's plan id.
        entries = _ops(inst)
        assert [e["op"] for e in entries] == ["dry_run", "apply"]
        assert entries[1]["plan_id"] == plan

    def test_reapply_is_refused(self, tmp_path):
        inst = tmp_path / "inst"
        plan = _report(_run(inst, *ACT))["plan_id"]
        assert _run(inst, *ACT, "--apply", "--plan", plan).returncode == 0
        r = _run(inst, *ACT, "--apply", "--plan", plan)
        assert r.returncode != 0
        assert plan in r.stderr
        assert "--undo --undo-plan" in r.stderr  # the auditable re-run path
        assert [e["op"] for e in _ops(inst)] == ["dry_run", "apply"]

    def test_undo_then_apply_is_allowed(self, tmp_path):
        inst = tmp_path / "inst"
        plan = _report(_run(inst, *ACT))["plan_id"]
        assert _run(inst, *ACT, "--apply", "--plan", plan).returncode == 0
        assert _run(inst, *ACT, "--undo", "--undo-plan",
                    plan).returncode == 0
        r = _run(inst, *ACT, "--apply", "--plan", plan)
        assert r.returncode == 0, r.stderr
        assert [e["op"] for e in _ops(inst)] == \
            ["dry_run", "apply", "undo", "apply"]


class TestE6StubTruthfulness:
    def test_cli_journal_never_claims_a_real_effect(self, tmp_path):
        # The CLI executor is a stub, so every entry it writes must say so
        # and the operator must be told in the same breath as ✓ APPLIED.
        inst = tmp_path / "inst"
        plan = _report(_run(inst, *ACT))["plan_id"]
        r_apply = _run(inst, *ACT, "--apply", "--plan", plan)
        assert r_apply.returncode == 0
        r_undo = _run(inst, *ACT, "--undo", "--undo-plan", plan)
        assert r_undo.returncode == 0
        entries = _ops(inst)
        assert entries[1]["op"] == "apply" and entries[1]["stub"] is True
        assert entries[2]["op"] == "undo" and entries[2]["stub"] is True
        assert "APPLIED" in r_apply.stdout
        assert "STUB" in r_apply.stdout and "no host change" in r_apply.stdout
        assert "STUB" in r_undo.stdout

    def test_library_default_is_not_stubbed(self, tmp_path):
        # A caller that wires a REAL side effect must not be marked stub.
        from neuralosd.actuate import actuate
        fired = []
        plan = actuate(*ACT[:0], action="block_ip", target="1.2.3.4",
                       reason="C2", operator="qa",
                       instance_dir=str(tmp_path / "i"))["plan_id"]
        actuate(action="block_ip", target="1.2.3.4", reason="C2",
                operator="qa", instance_dir=str(tmp_path / "i"),
                apply_plan=plan, side_effect=lambda: fired.append(1))
        entry = _ops(tmp_path / "i")[1]
        assert fired == [1]          # the real effect fired
        assert entry["stub"] is False


class TestArgparseDestContract:
    """E5 was a silent dest mismatch: the parser set `plan`, the shim read
    `apply_plan`. This test reads the shim's getattr(a, "name") literals
    out of the AST and asserts the parser sets every one of them."""

    SHIM = Path(__file__).resolve().parents[1] / "src" / "neuralosd" / \
        "_cmd" / "actuate.py"

    def _read_groups(self):
        """Groups of getattr(a, ...) names, one group per boolean
        expression: a standalone read is its own group; the E5-fix pattern
        `getattr(a, "apply_plan", None) or getattr(a, "plan", None)` is
        one group that resolves if ANY name in it is a real dest."""
        tree = ast.parse(self.SHIM.read_text(encoding="utf-8"))
        groups, chained = [], set()

        def is_getattr_on_a(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "a"
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str))

        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                members = [v for v in node.values if is_getattr_on_a(v)]
                if members:
                    groups.append([m.args[1].value for m in members])
                    chained.update(id(m) for m in members)
        for node in ast.walk(tree):
            if is_getattr_on_a(node) and id(node) not in chained:
                groups.append([node.args[1].value])
        return groups

    def test_every_flag_the_shim_reads_is_a_real_dest(self):
        from neuralosd import cli
        ap = cli.build_parser()
        a = ap.parse_args([
            "actuate", "--instance-dir", "x", "--action", "block_ip",
            "--target", "t", "--reason", "r", "--operator", "o",
            "--dry-run", "--apply", "--plan", "p", "--undo",
            "--undo-plan", "u"])
        groups = self._read_groups()
        assert groups, "AST scan found no getattr(a, ...) reads"
        unresolved = [g for g in groups
                      if not any(hasattr(a, n) for n in g)]
        assert not unresolved, (
            f"shim read chain(s) {unresolved} resolve to no argparse dest — "
            f"the E5 class of bug (silent dest mismatch)")
