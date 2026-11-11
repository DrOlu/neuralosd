"""`neuralosd slo` — refusal-rate SLO for one instance.

    neuralosd slo --instance-dir . --window-hours 24 --max-refusal-rate 0.4

A rising refusal rate is the menu-gap signal, quantified. Exit 1 above SLO.
"""
import json
import os


def run(a):
    from ..loop import refusal_rate
    stats = refusal_rate(os.path.abspath(a.instance_dir),
                         window_hours=a.window_hours,
                         max_refusal_rate=a.max_refusal_rate,
                         min_asks=getattr(a, "min_asks", None))
    print(json.dumps(stats, indent=2))
    if stats["insufficient_data"]:
        print(f"\n⚠ insufficient data: {stats['asks']} ask(s) in the last "
              f"{a.window_hours}h (need {stats['min_asks']}) — rate "
              f"{stats['refusal_rate']:.0%} is not a signal yet")
        return 0
    if stats["breach"]:
        print(f"\n✗ SLO BREACH: {stats['refusal_rate']:.0%} refused "
              f"({stats['refused']}/{stats['asks']} asks in the last "
              f"{a.window_hours}h) — mine the gaps: "
              f"neuralosd gaps <ask_audit.jsonl>")
        raise SystemExit(1)
    print(f"\n✓ within SLO ({stats['refusal_rate']:.0%} <= "
          f"{a.max_refusal_rate:.0%})")
