"""`neuralosd actuate` — named, journaled, reversible actions.

    neuralosd actuate --instance-dir . --action block_ip \\
        --target 1.2.3.4 --reason "C2 beacon" --operator analyst
    # ^ dry-run: validates, mints a plan_id, does NOT touch the system

    neuralosd actuate --instance-dir . --action block_ip \\
        --target 1.2.3.4 --reason "C2" --operator analyst \\
        --apply --plan <plan-id-from-dry-run>
    # ^ fires the side effect, journals the application

    neuralosd actuate --instance-dir . --action block_ip \\
        --target 1.2.3.4 --undo --plan <plan-id>
    # ^ reverses a previously applied action
"""
import json
import os
import sys


def run(a):
    from ..actuate import actuate

    instance_dir = os.path.abspath(a.instance_dir)
    action = getattr(a, "action", None)
    target = getattr(a, "target", None)
    reason = getattr(a, "reason", None)
    operator = getattr(a, "operator", None)
    # E5: the parser registers --plan (dest "plan") and --undo-plan
    # (dest "undo_plan"), but this shim used to read apply_plan /
    # undo_plan_id — attributes that NEVER exist. --apply --plan <id>
    # therefore always passed apply_plan=None, actuate() took the DRY-RUN
    # branch, and the CLI could not apply anything: every attempt minted a
    # fresh plan id, so the one-shot guard and the blind-actuation guard
    # were both unreachable from the documented entry point. Read the real
    # dests; the old names stay as fallbacks for embedders who built their
    # own parser.
    apply_plan = getattr(a, "apply_plan", None) or getattr(a, "plan", None)
    undo_plan_id = getattr(a, "undo_plan", None) or getattr(a, "undo_plan_id", None)

    if getattr(a, "dry_run", False) and not action:
        print("error: --action is required for dry-run", file=sys.stderr)
        return 2

    # E5: --apply without a plan is refused HERE. Without this check the
    # library only ever sees apply_plan=None and dry-runs — its own
    # blind-actuation error is unreachable from the CLI.
    if getattr(a, "apply", False) and not apply_plan:
        print("error: --apply needs --plan <id> from a seen dry-run — "
              "blind actuation is the pattern this module exists to prevent",
              file=sys.stderr)
        return 2

    # E6: the CLI's executor is a STUB — nothing touches the host. A
    # journal that says APPLIED while nothing happened is a lie an operator
    # will trust during an incident, so every entry written through this
    # path is stamped "stub": true (the library takes --stub for exactly
    # this) and the success notice says it out loud.
    side_effect = lambda: None  # production wires the real call

    try:
        report = actuate(
            action=action, target=target, reason=reason,
            operator=operator, instance_dir=instance_dir,
            apply_plan=apply_plan if apply_plan else None,
            undo_plan_id=undo_plan_id,
            side_effect=side_effect, stub=True)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("dry_run"):
        print(f"\n⚠ DRY-RUN — nothing has been done. Review the plan, "
              f"then:\n  neuralosd actuate --instance-dir {instance_dir} "
              f"--action {action} --target {target} \\\n"
              f"      --reason \"...\" --operator {operator} "
              f"--apply --plan {report['plan_id']}")
    elif report.get("undone"):
        print(f"\n✓ action undone and journaled")
        if report.get("stub"):
            print('⚠ STUB EXECUTOR — this build performed no host change; '
                  'the journal entry records "stub": true.')
    elif report.get("applied"):
        print(f"\n✓ APPLIED and journaled — rollback with:\n"
              f"  neuralosd actuate --instance-dir {instance_dir} "
              f"--action {action} --target {target} "
              f"--undo --plan {report['plan_id']}")
        if report.get("stub"):
            print('⚠ STUB EXECUTOR — this build performed no host change; '
                  'the journal entry records "stub": true.')
    return 0
