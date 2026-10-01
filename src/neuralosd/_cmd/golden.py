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
    with open(golden_path) as f:
        golden = json.load(f)

    passed = inst.golden_run(golden)
    total = len(golden.get("items", []))
    raise SystemExit(0 if passed == total else 1)