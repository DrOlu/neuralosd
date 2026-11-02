"""`neuralosd shadow` — shadow mode for generated probes.

    neuralosd shadow --instance-dir .                    # report
    neuralosd shadow --instance-dir . --evaluate         # one agreement run
    neuralosd shadow --instance-dir . --promote --min-agreement 0.9 \
        --min-runs 3 --max-age-days 14

A reason-installed probe installed with --shadow is NOT routable. It earns
its menu slot by agreeing with its recorded oracle across the shadow window.
Kills the "generated probe confidently wrong" class.
"""
import json
import os


def run(a):
    from ..loop import (shadow_evaluate, shadow_promote, shadow_report)

    instance_dir = os.path.abspath(a.instance_dir)

    if getattr(a, "evaluate", False) or getattr(a, "promote", False):
        names = shadow_report(instance_dir)["shadowed"]
        if not names:
            raise SystemExit("error: no shadowed probes in this instance")
    else:
        print(json.dumps(shadow_report(instance_dir), indent=2))
        return

    if getattr(a, "evaluate", False):
        for n in names:
            try:
                r = shadow_evaluate(instance_dir, n)
                mark = "AGREES" if r["agreed"] else "DISAGREES"
                print(f"  {n}: {mark}  (value {r['value']} vs oracle "
                      f"{r['oracle']})")
            except SystemExit as e:
                print(f"  {n}: {e}")

    if getattr(a, "promote", False):
        for n in names:
            ok = shadow_promote(instance_dir, n,
                                min_agreement=a.min_agreement,
                                min_runs=a.min_runs,
                                max_age_days=a.max_age_days)
            print(f"  {n}: {'PROMOTED — now routable' if ok else 'stays in shadow'}")
