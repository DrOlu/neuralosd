"""`neuralosd truth` — verify golden answers against the live data layer."""
import json
import os


def run(a):
    from ._common import load_instance
    from ..router import NoResults

    inst_dir = os.path.abspath(a.dir)
    inst = load_instance(inst_dir)
    golden_path = os.path.join(inst_dir, "golden.json")
    if not os.path.isfile(golden_path):
        raise SystemExit(f"error: no golden.json in {inst_dir}")
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)

    ok = bad = 0
    for item in golden.get("items", []):
        q = item["q"]
        try:
            env = inst.ask(q, full=True)
            probe = env.get("probe")
            results = env.get("results")
            good = (item.get("expect_probe") in (None, probe)
                    and results not in (None, [], {}))
        except NoResults:
            probe, good = None, False
        ok += good
        bad += (not good)
        print(f"{'TRUTH-OK' if good else 'TRUTH-FAIL'}  {q!r} -> {probe}")
    print(f"truth: {ok}/{ok + bad} verified")
    raise SystemExit(0 if bad == 0 else 1)