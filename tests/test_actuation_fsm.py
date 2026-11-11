"""F1-F5 — the actuation journal is an explicit, durable state machine.

The CLI is a NEW PROCESS every invocation, so the journal is the only
state there is. These tests pin the machine from the library entry point
(the CLI pins its own flag grammar in test_actuate_cli.py):

    (none) --dry_run--> DRY_RUN --apply--> APPLIED --undo--> UNDONE
                                                 ^              |
                                                 +---- apply ---+

F1  the one-shot guard was "has ever been undone", not "is undone now": one
    undo disabled it for every later apply, so apply/undo/apply/apply fired
    twice in a row.
F2  undo had no state guard: a second undo fired the reverse action again
    and journaled undone again.
F3  apply/undo never compared action/target with the reviewed plan, so a
    plan could be "applied" as a different action and target.
F4  one torn trailing line (a crash mid-append) crashed every later command.
F5  rows written before v2.9.10 have no "op" key and were invisible to the
    guard, so a pre-upgrade applied plan could be applied again.
"""
import json

import pytest

from neuralosd.actuate import actuate

ACT = dict(action="block_ip", target="203.0.113.9",
           reason="C2 beacon", operator="qa")
TS = 1_700_000_000.0


def _call(inst, **kw):
    """actuate() with a fixed clock — no wall-clock deltas in assertions."""
    args = dict(ACT)
    args.update(kw)
    args.setdefault("ts", TS)
    args.setdefault("instance_dir", str(inst))
    return actuate(**args)


def _ops(inst):
    path = inst / "actuation_journal.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_refused(exc, *needles):
    text = str(exc.value)
    for needle in needles:
        assert needle in text, text


# ── legal transitions ─────────────────────────────────────────────────────

def test_legal_path_dry_apply_undo_apply(tmp_path):
    fired = []
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan, side_effect=lambda: fired.append("apply"))
    _call(tmp_path, undo_plan_id=plan,
          side_effect=lambda: fired.append("undo"))
    _call(tmp_path, apply_plan=plan, side_effect=lambda: fired.append("apply"))
    assert fired == ["apply", "undo", "apply"]
    assert [e["op"] for e in _ops(tmp_path)] == \
        ["dry_run", "apply", "undo", "apply"]


def test_undo_from_undone_is_the_only_reverse(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan)
    _call(tmp_path, undo_plan_id=plan)
    # from UNDONE only APPLY is legal; a second undo is not
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, undo_plan_id=plan)
    _assert_refused(exc, "UNDONE", "undo refused", "reverse action twice")
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply", "undo"]


# ── F1: order-sensitive one-shot guard ───────────────────────────────────

def test_apply_from_applied_is_refused(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan)
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan)
    _assert_refused(exc, "APPLIED", "apply refused", "--undo")
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply"]


def test_one_earlier_undo_does_not_disable_the_guard(tmp_path):
    """F1: after apply/undo/apply the state is APPLIED again, so the next
    apply is illegal even though an undo exists SOMEWHERE in the journal."""
    fired = []
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan, side_effect=lambda: fired.append("a1"))
    _call(tmp_path, undo_plan_id=plan, side_effect=lambda: fired.append("u"))
    _call(tmp_path, apply_plan=plan, side_effect=lambda: fired.append("a2"))
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan,
              side_effect=lambda: fired.append("a3"))
    _assert_refused(exc, "APPLIED", "already")
    assert fired == ["a1", "u", "a2"]      # the third apply never fired


# ── F2: undo state guard ─────────────────────────────────────────────────

def test_double_undo_never_fires_twice(tmp_path):
    fired = []
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan)
    _call(tmp_path, undo_plan_id=plan, side_effect=lambda: fired.append("u1"))
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, undo_plan_id=plan,
              side_effect=lambda: fired.append("u2"))
    _assert_refused(exc, "UNDONE", "undo refused")
    assert fired == ["u1"]
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply", "undo"]


def test_undo_from_dry_run_is_refused(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, undo_plan_id=plan)
    _assert_refused(exc, "DRY_RUN", "undo refused", "never applied")


def test_undo_and_apply_unknown_plans_are_refused(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, undo_plan_id="plan-nope")
    _assert_refused(exc, "no applied action")
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan="plan-nope")
    _assert_refused(exc, "no dry-run found")
    assert _ops(tmp_path) == []


# ── F3: a plan is a promise about action + target ────────────────────────

def test_apply_cannot_substitute_the_action(tmp_path):
    fired = []
    plan = _call(tmp_path)["plan_id"]
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan, action="quarantine_file",
              side_effect=lambda: fired.append("wrong"))
    _assert_refused(exc, "DRY_RUN", "apply refused", "recorded action")
    assert fired == []
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run"]


def test_apply_cannot_substitute_the_target(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan, target="198.51.100.7")
    _assert_refused(exc, "apply refused", "recorded")


def test_undo_cannot_substitute_the_target(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan)
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, undo_plan_id=plan, target="198.51.100.7")
    _assert_refused(exc, "APPLIED", "undo refused", "recorded")
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply"]


# ── F4: torn / corrupt journal ───────────────────────────────────────────

def test_torn_trailing_line_is_tolerated_and_repaired(tmp_path):
    """A crash mid-append leaves a partial last line. State before it is
    still authoritative, and the next write repairs the artifact."""
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan)
    path = tmp_path / "actuation_journal.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"op": "apply", "plan_id": "torn')      # crash here
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan)                  # still APPLIED
    _assert_refused(exc, "APPLIED")
    # a legal transition afterwards reproduces a clean journal
    plan2 = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan2)
    rows = _ops(tmp_path)
    assert [e["op"] for e in rows] == ["dry_run", "apply", "dry_run", "apply"]


def test_corrupt_middle_line_is_refused_by_name(tmp_path):
    """Damage followed by MORE rows is not a crash artifact: rows may have
    been lost or reordered, so the machine refuses instead of guessing."""
    plan = _call(tmp_path)["plan_id"]
    path = tmp_path / "actuation_journal.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        # a valid row AFTER the damage makes it a middle-line corruption
        fh.write(json.dumps({"op": "dry_run", "plan_id": "plan-other",
                             "action": "block_ip", "target": "t",
                             "dry_run": True, "applied": False}) + "\n")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan)
    _assert_refused(exc, "corrupt at line", "partial history")
    assert path.read_text(encoding="utf-8") == before   # nothing appended


# ── F5: pre-v2.9.10 rows carry no "op" ───────────────────────────────────

def test_legacy_applied_row_blocks_reapply(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    path = tmp_path / "actuation_journal.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"plan_id": plan, "action": "block_ip",
                             "target": "203.0.113.9", "reason": "C2",
                             "operator": "qa", "applied": True,
                             "dry_run": False}) + "\n")
    with pytest.raises(SystemExit) as exc:
        _call(tmp_path, apply_plan=plan)
    _assert_refused(exc, "APPLIED", "apply refused")


def test_legacy_dry_run_row_still_allows_apply(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    path = tmp_path / "actuation_journal.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"plan_id": plan, "action": "block_ip",
                             "target": "203.0.113.9", "reason": "C2",
                             "operator": "qa", "applied": False,
                             "dry_run": True}) + "\n")
    _call(tmp_path, apply_plan=plan)
    assert _ops(tmp_path)[-1]["op"] == "apply"


# ── the journal never claims an unoccurred effect ────────────────────────

def test_failing_apply_effect_is_not_journaled(tmp_path):
    plan = _call(tmp_path)["plan_id"]

    def boom():
        raise RuntimeError("host unreachable")

    with pytest.raises(RuntimeError):
        _call(tmp_path, apply_plan=plan, side_effect=boom)
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run"]
    # the plan is still APPLY-able: no effect was claimed
    _call(tmp_path, apply_plan=plan)
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply"]


def test_failing_undo_effect_is_not_journaled(tmp_path):
    plan = _call(tmp_path)["plan_id"]
    _call(tmp_path, apply_plan=plan)

    def boom():
        raise RuntimeError("undo failed")

    with pytest.raises(RuntimeError):
        _call(tmp_path, undo_plan_id=plan, side_effect=boom)
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply"]
    _call(tmp_path, undo_plan_id=plan)       # still retryable
    assert [e["op"] for e in _ops(tmp_path)] == ["dry_run", "apply", "undo"]


# ── unicode survives the journal round trip ──────────────────────────────

def test_journal_rows_are_utf8_round_trips(tmp_path):
    plan = _call(tmp_path, reason="C2 beacon — naïve café",
                 operator="Zoë")["plan_id"]
    _call(tmp_path, apply_plan=plan, reason="rollout café",
          operator="Zoë")
    dry, applied = _ops(tmp_path)[0], _ops(tmp_path)[-1]
    assert dry["reason"] == "C2 beacon — naïve café"
    assert applied["reason"] == "rollout café"
    assert applied["operator"] == "Zoë"
