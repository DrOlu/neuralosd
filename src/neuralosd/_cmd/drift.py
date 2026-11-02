"""`neuralosd drift` — schema-drift-triggered re-audit.

    neuralosd drift --instance-dir . --source ./new_dump.csv

Re-profiles the source with the same profilers `init` uses and flags every
probe the NEW schema breaks — name-caged probes (total_<col>, distinct_<col>,
average_<col>) whose column is gone, and enum-caged probes whose values no
longer exist. Drift is an audit trigger, not a 3am page: this runs BEFORE
the probes fail at runtime. Exits 1 when the instance needs a re-audit.
"""
import json
import os


def run(a):
    from ..loop import audit_drift

    report = audit_drift(os.path.abspath(a.instance_dir), a.source)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["stale_probes"]:
        print(f"\n✗ {len(report['stale_probes'])} probe(s) are broken by "
              f"this schema change. {report['action']}")
        raise SystemExit(1)
    print("\n✓ no probe is broken by this schema change")
