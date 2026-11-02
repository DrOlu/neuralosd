"""`neuralosd mothball` — menu hygiene: demote probes nobody queries.

    neuralosd mothball --instance-dir . --age-days 180 --apply
    neuralosd mothball --instance-dir . --reinstate row_count

Mothballed probes are KEPT on disk and reinstatable — they just leave the
menu. Tight menus route better; mothballing every probe is refused (a mute
instance is worse than a stale one).
"""
import json
import os


def run(a):
    from ..loop import mothball, reinstate

    instance_dir = os.path.abspath(a.instance_dir)

    if getattr(a, "reinstate", None):
        ok = reinstate(instance_dir, a.reinstate)
        print(f"{'reinstated' if ok else 'was not mothballed'}: "
              f"{a.reinstate}")
        return

    report = mothball(instance_dir, age_days=a.age_days,
                      apply=bool(a.apply))
    mode = "MOTHBALLED" if a.apply else "candidates (dry run — add --apply)"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if a.apply and report["mothballed"]:
        print(f"\n{mode}: {', '.join(report['mothballed'])}\n"
              f"reinstate with: neuralosd mothball --instance-dir "
              f"{instance_dir} --reinstate <name>")
