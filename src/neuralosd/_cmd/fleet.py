"""`neuralosd fleet` — multi-instance health dashboard.

    neuralosd fleet ~/instances/* --window-hours 24 --max-refusal-rate 0.4

Per-instance: menu size, asks, refusal rate vs SLO, mothballed count.
Exit 1 when any instance breaches — this is the page, not a vibe.
"""
import json
import sys


def run(a):
    from ..loop import fleet_report
    dirs = []
    for pattern in a.dirs:
        import glob
        dirs.extend(sorted(glob.glob(pattern)) or [pattern])
    report = fleet_report(dirs, window_hours=a.window_hours,
                          max_refusal_rate=a.max_refusal_rate,
                          min_asks=getattr(a, "min_asks", None))
    print(f"{'INSTANCE':<28} {'PROBES':>6} {'ASKS':>6} {'REFUSED':>7} "
          f"{'RATE':>6} {'SLO':>5} MOTHBALLED")
    for i in report["instances"]:
        status = ("n/a" if i["insufficient_data"]
                  else "BREACH" if i["breach"] else "ok")
        print(f"{i['instance']:<28} {str(i['probes']):>6} {i['asks']:>6} "
              f"{i['refused']:>7} {i['refusal_rate']:>6.0%} {status:>5} "
              f"{i['mothballed']}")
    print(json.dumps(report, indent=2))
    if report["breached"]:
        print(f"\n✗ {report['breached']} instance(s) breach the refusal SLO")
        raise SystemExit(1)
    summary = f"\n✓ {report['healthy']} instance(s) within SLO"
    if report["undersized"]:
        summary += (f" · {report['undersized']} undersized (n/a) — "
                    f"{report['breached']} breach")
    print(summary)
