"""`neuralosd lint` — trigger-collision linter over a menu, instance, or dir."""
import json
import os


def run(a):
    from ..lint import TriggerLinter

    target = a.menu
    if os.path.isdir(target) or target.endswith(".py"):
        from ._common import load_instance
        inst = load_instance(target)
        menu = inst.menu
    elif os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        menu = data.get("menu", data) if isinstance(data, dict) else data
    else:
        raise SystemExit(f"error: no such menu/instance: {target}")

    report = TriggerLinter(menu, top=a.top).lint()
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    collisions = report.get("collisions", report.get("collision_groups", [])) \
        if isinstance(report, dict) else []
    if collisions:
        raise SystemExit(1)