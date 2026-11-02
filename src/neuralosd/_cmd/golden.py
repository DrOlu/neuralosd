"""`neuralosd golden` — run / grow the golden question bank.

v2.3.0: oracle provenance. `--record` appends a case WITH its receipt (who
verified the value, when, from what source, how) and `--receipts` prints the
evidence chain — an auditor asks "why is 214 the right answer?" and gets the
receipt, not just the number.
"""
import json
import os
import sys


def run(a):
    from ._common import load_instance

    inst_dir = os.path.abspath(a.dir)

    # --record: append a case with its oracle receipt
    if getattr(a, "record", False):
        from ..banks import record_golden
        q = " ".join(getattr(a, "question", None) or [])
        if not q:
            raise SystemExit("error: --record needs --question")
        item = record_golden(
            inst_dir, q=q,
            expect_probe=getattr(a, "expect_probe", None),
            expect_refusal=bool(getattr(a, "expect_refusal", False)),
            value=getattr(a, "value", None),
            by=getattr(a, "by", None),
            source=getattr(a, "source", None),
            method=getattr(a, "method", None))
        print(json.dumps(item, indent=2, ensure_ascii=False))
        print("recorded with receipt — re-run `neuralosd golden --dir ...` "
              "to prove it routes")
        return

    # --receipts: the evidence chain, for the auditor
    if getattr(a, "receipts", False):
        from ..banks import receipts
        print(receipts(inst_dir))
        return

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
