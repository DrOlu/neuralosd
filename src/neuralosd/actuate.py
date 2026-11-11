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

The journal is the durable state machine (the CLI is a new process for
every command), so every decision below is derived from it, never from
process memory. States per plan_id:

    (none) --dry_run--> DRY_RUN --apply--> APPLIED --undo--> UNDONE
                                                   ^              |
                                                   +---- apply ---+

Illegal transitions are refused BY NAME (see `_illegal`), never
repaired silently and never forced. A plan is APPLIED/UNDONE according to
its LAST transition, not "has ever been": a single earlier undo must not
disable the one-shot guard for every later apply (F1), and a second undo
must not fire the reverse action twice (F2).

Journal compatibility: rows written before v2.9.10 carry no "op" key; a
row with `dry_run: true` reads as a dry-run and a row with `applied: true`
reads as an apply, so an old journal still guards against a duplicate
fire (F5). A single torn TRAILING line is the normal result of a crash
mid-append and is ignored; malformed data anywhere else refuses by name
rather than being guessed around (F4).
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

# The legal transitions, one table shared by the guard, the error text and
# the audit doc (docs/audits/state-machines.md). Keys are current states;
# values are the ops allowed from that state.
LEGAL_TRANSITIONS = {
    None: {"dry_run"},
    "dry_run": {"apply"},
    "applied": {"undo"},
    "undone": {"apply"},
}


def _journal_path(instance_dir):
    return os.path.join(instance_dir, "actuation_journal.jsonl")


def _repair_torn_tail(path):
    """Close the crash window before appending to a torn journal.

    A crash can leave a partial last line with no newline. Reading tolerates
    it, but if the next command appends after it the torn bytes become a
    MIDDLE line and every later read would refuse. Repair the artifact at
    the one place that writes: terminate a complete-but-unterminated tail,
    truncate an unparseable one (it was never a committed row).
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return
    if not data or data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n") + 1
    tail = data[cut:]
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        with open(path, "r+b") as fh:
            fh.truncate(cut)
            fh.flush()
            os.fsync(fh.fileno())
    else:
        with open(path, "ab") as fh:
            fh.write(b"\n")
            fh.flush()
            os.fsync(fh.fileno())


def _journal(instance_dir, entry):
    """Append one journal row durably.

    One write + fsync: a crash loses at most the trailing partial line,
    which `_read_journal` knows how to recognise as a torn append instead
    of crashing every later command.
    """
    path = _journal_path(instance_dir)
    _repair_torn_tail(path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _op_of(entry):
    """The operation a journal row records, including legacy rows.

    v2.9.10 and earlier wrote rows WITHOUT "op"; infer it from the same
    flags the old writer set so an old journal is still a state machine.
    """
    op = entry.get("op")
    if op:
        return op
    if entry.get("dry_run"):
        return "dry_run"
    if entry.get("applied"):
        return "apply"
    if entry.get("undone"):
        return "undo"
    return None


def _read_journal(instance_dir):
    """(entries, error) — tolerant ONLY of a torn trailing append.

    Returns the parsed rows and None, or the parsed prefix and a message
    naming the corruption. A malformed line is acceptable exactly when it
    is the last non-blank line (a crash between write and newline); the
    same damage in the middle means rows were lost or reordered, and the
    state machine must refuse rather than reason from a partial history.
    """
    path = _journal_path(instance_dir)
    if not os.path.isfile(path):
        return [], None
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    entries = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            if any(l.strip() for l in lines[i + 1:]):
                return entries, (f"actuation journal is corrupt at line {i + 1} "
                                 f"and more rows follow it — refusing to "
                                 f"derive state from a partial history")
            continue          # crash mid-append: the last row was never written
        if not isinstance(obj, dict):
            if any(l.strip() for l in lines[i + 1:]):
                return entries, (f"actuation journal line {i + 1} is not a "
                                 f"JSON object and more rows follow it")
            continue
        entries.append(obj)
    return entries, None


def _plan_state(entries, plan_id):
    """(state, identity): the plan's LAST transition and who it promises to touch.

    state is None if the plan has no rows. `identity` is the most recent
    dry-run/apply row — the one that DECLARED action/target — so an undone
    plan is still compared against what was applied, not against whatever
    an older writer may have put in its undo row.
    """
    state, identity = None, None
    for entry in entries:
        if entry.get("plan_id") != plan_id:
            continue
        op = _op_of(entry)
        if op == "dry_run" and state is None:
            state, identity = "dry_run", entry
        elif op == "apply":
            state, identity = "applied", entry
        elif op == "undo":
            state = "undone"
    return state, identity


def _illegal(plan_id, state, op, detail, hint):
    label = state.upper() if state else "UNKNOWN"
    raise SystemExit(
        f"error: plan {plan_id!r} is {label} — {op} refused "
        f"({detail}; legal from here: "
        f"{', '.join(sorted(LEGAL_TRANSITIONS.get(state, set()))) or 'none'}). "
        f"{hint}")


def _same_plan(entry, action, target):
    """The action/target named now must be the ones the plan recorded.

    A plan is a promise about WHAT will be touched; accepting a different
    action/target under the same plan_id journals an effect that was never
    reviewed (F3). Reason/operator are per-entry attribution and may differ
    (a second operator may undo what the first applied).
    """
    return (str(entry.get("action")) == str(action)
            and str(entry.get("target")) == str(target))


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
    journal, corrupt = _read_journal(instance_dir)
    if corrupt:
        raise SystemExit(f"error: {corrupt}. Archive or repair "
                         f"{_journal_path(instance_dir)} before actuating.")

    # ── UNDO ──────────────────────────────────────────────────────────
    if undo_plan_id:
        state, applied = _plan_state(journal, undo_plan_id)
        if state != "applied":
            if state is None:
                raise SystemExit(
                    f"error: no applied action with plan_id "
                    f"{undo_plan_id!r}")
            if state == "undone":
                _illegal(undo_plan_id, state, "undo",
                         "a second undo would fire the reverse action twice",
                         "The plan is already reversed; apply it again "
                         "(--apply --plan) if that is what you mean.")
            _illegal(undo_plan_id, state, "undo",
                     "the plan was never applied",
                     "Only an APPLIED plan can be undone.")
        if not _same_plan(applied, action, target):
            _illegal(undo_plan_id, state, "undo",
                     f"the plan recorded action={applied.get('action')!r} "
                     f"target={applied.get('target')!r}, not "
                     f"action={action!r} target={target!r}",
                     "Undo the plan you reviewed; start a new dry-run for a "
                     "different action or target.")
        # Fire BEFORE journaling: the journal may never claim an effect
        # that did not occur, so a failing side effect leaves the plan
        # APPLIED (retryable) with no undo row.
        if side_effect is not None:
            side_effect()
        undo_entry = {
            "op": "undo", "plan_id": undo_plan_id,
            "action": action, "target": target,
            "reason": reason, "operator": operator, "ts": ts,
            "undone": True, "stub": bool(stub),
            "original_reason": applied.get("reason"),
        }
        _journal(instance_dir, undo_entry)
        return {"undone": True, "plan_id": undo_plan_id,
                "action": action, "target": target, "stub": bool(stub)}

    # ── DRY-RUN or APPLY ──────────────────────────────────────────────
    if apply_plan is not None:
        # APPLY: the operator saw the dry-run and is pulling the trigger
        if apply_plan is True or apply_plan == "":
            raise SystemExit(
                "error: --apply needs --plan <id> from a seen dry-run — "
                "blind actuation is the pattern this module exists to "
                "prevent")
        state, record = _plan_state(journal, apply_plan)
        if state is None:
            raise SystemExit(
                f"error: no dry-run found with plan_id {apply_plan!r} — "
                f"run the dry-run first, review the plan, then --apply")
        if state == "applied":
            _illegal(apply_plan, state, "apply",
                     "the plan has already been applied — re-applying "
                     "would fire the action twice",
                     f"Undo it first (--undo --undo-plan {apply_plan}) if "
                     f"you really mean to re-run it.")
        # The reviewed plan is a promise about WHAT will be touched; a
        # different action/target under its id was never reviewed (F3).
        # Checked from DRY_RUN and from UNDONE (against the last apply).
        if not _same_plan(record, action, target):
            _illegal(apply_plan, state, "apply",
                     f"the dry-run recorded action="
                     f"{record.get('action')!r} target="
                     f"{record.get('target')!r}, not action={action!r} "
                     f"target={target!r}",
                     "Run a new dry-run for the action you actually "
                     "mean to apply.")
        # Fire BEFORE journaling: a side effect that raises leaves the plan
        # in its current state (retryable) and writes no claim of success.
        if side_effect is not None:
            side_effect()
        entry = {"op": "apply", "plan_id": apply_plan,
                 "action": action, "target": target, "reason": reason,
                 "operator": operator, "ts": ts, "applied": True,
                 "dry_run": False, "stub": bool(stub)}
        _journal(instance_dir, entry)
        return {"applied": True, "plan_id": apply_plan, "action": action,
                "target": target, "dry_run": False, "stub": bool(stub)}

    # DRY-RUN: nothing touches the system
    plan_id = "plan-" + uuid.uuid4().hex[:12]
    entry = {"op": "dry_run", "plan_id": plan_id,
             "action": action, "target": target, "reason": reason,
             "operator": operator, "ts": ts, "applied": False,
             "dry_run": True, "stub": bool(stub)}
    _journal(instance_dir, entry)
    return {"dry_run": True, "applied": False, "plan_id": plan_id,
            "action": action, "target": target, "reason": reason,
            "description": ACTIONS[action]}
