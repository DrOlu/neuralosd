"""E4 — actuation is ONE-SHOT: dry_run -> applied -> undone -> applied.

On v2.9.8 the APPLY branch only checked that a dry-run with the plan_id
existed; nothing refused an applied -> applied transition, so a replayed
--apply fired the side effect again (duplicate firewall rule, a host
isolation applied twice — whose second application can defeat the first
undo — a credential rotated twice). These tests pin the guard: the state
is read from the journal (durable — the CLI is a new process every time),
an applied plan refuses until undone, and undo-then-apply is the one
auditable re-run path. There is deliberately no --force.
"""
import json

import pytest

from neuralosd.actuate import actuate


def _journal(instance_dir):
    with open(instance_dir / "actuation_journal.jsonl", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_apply_then_reapply_is_refused(tmp_path):
    fired = []
    inst = str(tmp_path / "inst")

    plan = actuate("block_ip", "203.0.113.9", "SSH brute force", "ada",
                   inst)["plan_id"]
    actuate("block_ip", "203.0.113.9", "SSH brute force", "ada", inst,
            apply_plan=plan, side_effect=lambda: fired.append(1))
    assert fired == [1], "first apply must fire exactly once"

    with pytest.raises(SystemExit) as ei:
        actuate("block_ip", "203.0.113.9", "SSH brute force", "ada", inst,
                apply_plan=plan, side_effect=lambda: fired.append(1))
    assert fired == [1], "the replayed --apply must NOT fire the side effect"
    assert plan in str(ei.value), "refusal must name the plan"
    assert "already been applied" in str(ei.value)


def test_reapply_after_undo_is_allowed(tmp_path):
    fired = []
    inst = str(tmp_path / "inst")

    plan = actuate("block_ip", "203.0.113.9", "SSH brute force", "ada",
                   inst)["plan_id"]
    actuate("block_ip", "203.0.113.9", "SSH brute force", "ada", inst,
            apply_plan=plan, side_effect=lambda: fired.append("apply1"))
    actuate("block_ip", "203.0.113.9", "undo: false positive", "ada", inst,
            undo_plan_id=plan, side_effect=lambda: fired.append("undo"))
    actuate("block_ip", "203.0.113.9", "attacker returned", "ada", inst,
            apply_plan=plan, side_effect=lambda: fired.append("apply2"))
    # dry_run -> applied -> undone -> applied is the only legal path and it
    # must stay open: undo-then-apply is the auditable re-run route.
    assert fired == ["apply1", "undo", "apply2"]


def test_apply_requires_a_seen_dry_run(tmp_path):
    fired = []
    inst = str(tmp_path / "inst")
    with pytest.raises(SystemExit):
        actuate("block_ip", "203.0.113.9", "reason", "ada", inst,
                apply_plan="plan-neverseen",
                side_effect=lambda: fired.append(1))
    assert fired == []


def test_apply_without_plan_id_is_refused(tmp_path):
    fired = []
    inst = str(tmp_path / "inst")
    for bad in (True, ""):
        with pytest.raises(SystemExit):
            actuate("block_ip", "203.0.113.9", "reason", "ada", inst,
                    apply_plan=bad, side_effect=lambda: fired.append(1))
    assert fired == []


def test_journal_records_every_transition(tmp_path):
    inst = str(tmp_path / "inst")
    plan = actuate("block_ip", "203.0.113.9", "SSH brute force", "ada",
                   inst)["plan_id"]
    actuate("block_ip", "203.0.113.9", "SSH brute force", "ada", inst,
            apply_plan=plan)
    actuate("block_ip", "203.0.113.9", "undo: false positive", "ada", inst,
            undo_plan_id=plan)
    ops = [e["op"] for e in _journal(tmp_path / "inst")]
    assert ops == ["dry_run", "apply", "undo"]
    assert all(e.get("plan_id") == plan
               for e in _journal(tmp_path / "inst"))


def test_two_different_plans_are_independent(tmp_path):
    fired = []
    inst = str(tmp_path / "inst")
    plan_a = actuate("block_ip", "203.0.113.9", "reason a", "ada",
                     inst)["plan_id"]
    plan_b = actuate("block_ip", "198.51.100.7", "reason b", "ada",
                     inst)["plan_id"]
    actuate("block_ip", "203.0.113.9", "reason a", "ada", inst,
            apply_plan=plan_a, side_effect=lambda: fired.append("a"))
    # plan A applied must not block plan B — the guard is per plan_id.
    actuate("block_ip", "198.51.100.7", "reason b", "ada", inst,
            apply_plan=plan_b, side_effect=lambda: fired.append("b"))
    assert fired == ["a", "b"]
    assert len(_journal(tmp_path / "inst")) == 4  # 2 dry_runs + 2 applies
