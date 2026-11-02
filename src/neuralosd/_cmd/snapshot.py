"""`neuralosd snapshot` — pin a hashed snapshot of every probe's observation.

    neuralosd snapshot --instance-dir . --label q3
    neuralosd ask --instance-dir . --as-of q3 "units per row"

The pin freezes observations under a content hash; `--as-of` answers from
the pin even after the source changes. Answers become reproducible evidence.
"""
import json
import os


def run(a):
    from ..loop import pin_snapshot
    report = pin_snapshot(os.path.abspath(a.instance_dir), a.label)
    print(json.dumps(report, indent=2))
    print(f"\nanswer from this pin:  neuralosd ask --instance-dir "
          f"{a.instance_dir} --as-of {a.label} \"question\"")
