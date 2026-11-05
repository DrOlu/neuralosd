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
    apply_plan = getattr(a, "apply_plan", None)
    undo_plan_id = getattr(a, "undo_plan", None)

    if getattr(a, "dry_run", False) and not action:
        print("error: --action is required for dry-run", file=sys.stderr)
        return 2

    side_effect = None
    if apply_plan is not None and apply_plan is not False:
        side_effect = lambda: None  # production wires the real call
        apply_plan = apply_plan if isinstance(apply_plan, str) else \
            (apply_plan if apply_plan else True)

    try:
        report = actuate(
            action=action, target=target, reason=reason,
            operator=operator, instance_dir=instance_dir,
            apply_plan=apply_plan if apply_plan else None,
            undo_plan_id=undo_plan_id,
            side_effect=side_effect)
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
    elif report.get("applied"):
        print(f"\n✓ APPLIED and journaled — rollback with:\n"
              f"  neuralosd actuate --instance-dir {instance_dir} "
              f"--action {action} --target {target} "
              f"--undo --plan {report['plan_id']}")
    return 0
