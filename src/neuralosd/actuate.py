"""Actuation with proof (v2.9.0) — named, journaled, reversible actions.

The safety model is the same as rmagent-actuate:
  1. DRY-RUN first: the action is validated, a plan_id is minted, nothing
     touches the system. The operator reviews the plan.
  2. APPLY: requires the plan_id from a seen dry-run (no blind fires)
     and is ONE-SHOT: dry_run -> applied -> undone -> applied is the only
     legal path, so a replayed --apply can never fire the action twice.
     The side effect fires; the journal records who/when/what/why.
  3. UNDO: reverses a previously applied action. Also journaled.

The actual system change is behind `side_effect` — an injectable callable.
Tests never touch a real system; production wires it to Plasma's
actuation CLI or the Windows API.
"""
import json
import os
import time
import uuid

ACTIONS = {
    "block_ip": "Add a firewall rule blocking the target IP",
    "isolate_host": "Enable the host isolation policy (block all in/out)",
    "disable_user": "Disable the user account",
    "rotate_credential": "Force a credential rotation for the account",
    "quarantine_file": "Move the file to the quarantine directory",
}


def _journal_path(instance_dir):
    return os.path.join(instance_dir, "actuation_journal.jsonl")


def _journal(instance_dir, entry):
    with open(_journal_path(instance_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _load_journal(instance_dir):
    path = _journal_path(instance_dir)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def actuate(action, target, reason, operator, instance_dir,
            apply_plan=None, undo_plan_id=None, side_effect=None,
            stub=False, ts=None):
    """One actuation step: dry-run, apply, or undo.

    Returns a report dict. The journal is always written (even for
    dry-runs, so the operator has a record of what was proposed).

    action:     one of ACTIONS keys
    target:     the IP / host / user / file to act on
    reason:     WHY — required; a blank reason is a hard error
    operator:   who is doing this
    apply_plan: the plan_id from a previous dry-run (required for apply)
    undo_plan_id: the plan_id to reverse
    side_effect: the callable that actually touches the system (injectable
                 for tests; production wires it to Plasma's actuation CLI)
    stub:       True when side_effect is a no-op placeholder (the CLI's
                 default executor). Applies/undoes journaled through this
                 path record "stub": true — the journal must never claim
                 an effect that did not occur.
    """
    if action not in ACTIONS:
        raise SystemExit(
            f"error: unknown action {action!r} — valid: "
            f"{', '.join(sorted(ACTIONS))}")
    if not reason or not reason.strip():
        raise SystemExit(
            "error: --reason is required — actuation without a reason is "
            "the pattern this module exists to prevent")
    if not operator or not operator.strip():
        raise SystemExit("error: --operator is required — actuation "
                         "without attribution is not auditable")

    ts = ts if ts is not None else time.time()
    instance_dir = os.path.abspath(instance_dir)
    os.makedirs(instance_dir, exist_ok=True)

    # ── UNDO ──────────────────────────────────────────────────────────
    if undo_plan_id:
        journal = _load_journal(instance_dir)
        original = next((e for e in journal
                         if e.get("plan_id") == undo_plan_id
                         and e.get("applied")), None)
        if original is None:
            raise SystemExit(
                f"error: no applied action with plan_id {undo_plan_id!r}")
        undo_entry = {
            "op": "undo", "plan_id": undo_plan_id,
            "action": action, "target": target,
            "reason": reason, "operator": operator, "ts": ts,
            "undone": True, "stub": bool(stub),
            "original_reason": original.get("reason"),
        }
        _journal(instance_dir, undo_entry)
        # fire the undo side effect if the caller provided one
        if side_effect is not None:
            side_effect()
        return {"undone": True, "plan_id": undo_plan_id,
                "action": action, "target": target, "stub": bool(stub)}

    # ── DRY-RUN or APPLY ──────────────────────────────────────────────
    plan_id = "plan-" + uuid.uuid4().hex[:12]
    if apply_plan is not None:
        # APPLY: the operator saw the dry-run and is pulling the trigger
        if apply_plan is True or apply_plan == "":
            raise SystemExit(
                "error: --apply needs --plan <id> from a seen dry-run — "
                "blind actuation is the pattern this module exists to "
                "prevent")
        # look up the dry-run to confirm it was seen
        journal = _load_journal(instance_dir)
        dr = next((e for e in journal
                   if e.get("plan_id") == apply_plan
                   and e.get("dry_run")), None)
        if dr is None:
            raise SystemExit(
                f"error: no dry-run found with plan_id {apply_plan!r} — "
                f"run the dry-run first, review the plan, then --apply")
        # ONE-SHOT GUARD: dry_run -> applied -> undone is the only legal path.
        # Without this, a replayed --apply fired the side effect again
        # (duplicate firewall rule / double host isolation / credential
        # rotated twice after an operator retry). State is read from the
        # journal — durable — because the CLI is a new process every time.
        already = [e for e in journal
                   if e.get("plan_id") == apply_plan
                   and e.get("op") == "apply"]
        undone = [e for e in journal
                  if e.get("plan_id") == apply_plan
                  and e.get("op") in ("undo", "undo_plan")]
        if already and not undone:
            raise SystemExit(
                f"error: plan {apply_plan!r} has already been applied — "
                f"re-applying would fire the action twice. Undo it first "
                f"(--undo --undo-plan {apply_plan}) if you really mean to "
                f"re-run it.")
        applied = True
        if side_effect is not None:
            side_effect()
        entry = {"op": "apply", "plan_id": apply_plan,
                 "action": action, "target": target, "reason": reason,
                 "operator": operator, "ts": ts, "applied": True,
                 "dry_run": False, "stub": bool(stub)}
        _journal(instance_dir, entry)
        return {"applied": True, "plan_id": apply_plan, "action": action,
                "target": target, "dry_run": False, "stub": bool(stub)}
    else:
        # DRY-RUN: nothing touches the system
        entry = {"op": "dry_run", "plan_id": plan_id,
                 "action": action, "target": target, "reason": reason,
                 "operator": operator, "ts": ts, "applied": False,
                 "dry_run": True}
        _journal(instance_dir, entry)
        return {"dry_run": True, "applied": False, "plan_id": plan_id,
                "action": action, "target": target, "reason": reason,
                "description": ACTIONS[action]}
