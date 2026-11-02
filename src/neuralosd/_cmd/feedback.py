"""`neuralosd feedback` — the path from a user thumbs-down to the banks.

    neuralosd feedback --instance-dir . --question "units by region" \
        --verdict bad --correction "breakdown of units by region" --by ada
    neuralosd feedback --instance-dir . --verdict invalid --question "delete all rows"
    neuralosd feedback --instance-dir . --apply

A rejected answer is the highest-signal data there is. 'bad' records a
golden CANDIDATE (with the correction attached) — it is not a trap, because
the question is legitimate, the answer was wrong. 'invalid' records a trap
candidate, promoted only when the refusal is PROVEN at apply time.
"""
import json
import os
import time


def run(a):
    from ..banks import apply_feedback as _apply
    from ..loop import ingest_feedback

    instance_dir = os.path.abspath(a.instance_dir)

    if getattr(a, "apply", False):
        summary = _apply(instance_dir)
        print(json.dumps(summary, indent=2))
        return

    q = " ".join(a.question or [])
    if not q:
        raise SystemExit("error: feedback needs --question (or --apply)")
    verdict = a.verdict
    if verdict not in ("bad", "invalid"):
        raise SystemExit("error: --verdict must be 'bad' (wrong answer) or "
                         "'invalid' (out-of-scope question)")
    entry = ingest_feedback(instance_dir, question=q, verdict=verdict,
                            correction=a.correction, by=a.by,
                            ts=time.time())
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    kind = entry["kind"].replace("_", " ")
    print(f"\nrecorded as a {kind}. "
          f"Review with: neuralosd feedback --instance-dir {instance_dir} --apply")
