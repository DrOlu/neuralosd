"""`neuralosd calibrate` — learn per-probe confidence gates from a golden bank.

Runs the golden questions locally (no server needed) and, for each probe,
reports the minimum observed routing score so a conf_gate can be chosen.
"""
import json
import os


def run(a):
    from ._common import load_instance
    from ..router import tokens, score_probe

    golden_path = a.golden
    if not os.path.isfile(golden_path):
        raise SystemExit(f"error: no golden file: {golden_path}")
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)

    # instance dir is the golden file's directory (or cwd)
    inst_dir = os.path.dirname(os.path.abspath(golden_path)) or "."
    inst = load_instance(inst_dir)
    metas = {m.name: m for m in inst.router.metas}

    best = {}
    for item in golden.get("items", []):
        q = item["q"]
        expect = item.get("expect_probe")
        qt = tokens(q)
        scores = sorted(((score_probe(m, qt), m.name) for m in inst.router.metas),
                        reverse=True)
        top_score, top_name = scores[0]
        if expect and top_name == expect:
            best[expect] = min(best.get(expect, 1.0), top_score)

    out = {"instance": inst.name,
           "gates": {name: round(score, 3) for name, score in sorted(best.items())}}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    print(f"\ncalibration written to {a.out}", file=__import__("sys").stderr)