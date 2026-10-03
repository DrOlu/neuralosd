"""`neuralosd golden` — run the golden question bank for an instance."""
import json
import os
import sys


def run(a):
    from ._common import load_instance

    inst_dir = os.path.abspath(a.dir)
    inst = load_instance(inst_dir)

    golden_path = os.path.join(inst_dir, "golden.json")
    if not os.path.isfile(golden_path):
        raise SystemExit(f"error: no golden.json in {inst_dir}")
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)

    summary = inst.golden_run(golden)
    # A confidently answered trap fails the run outright, whatever the score.
    raise SystemExit(0 if (summary["wrong"] == 0
                           and summary["answered_traps"] == 0) else 1)