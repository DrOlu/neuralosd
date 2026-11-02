"""`neuralosd synonyms` — cluster-driven trigger proposals.

    neuralosd synonyms --instance-dir . [--audit ask_audit.jsonl]
    neuralosd synonyms --instance-dir . --apply synonym_proposals.json

Gap clusters become PROPOSED triggers for the probe that most plausibly
owns the pattern (audit score vectors + caged enums + name evidence).
Human-approved by default; approved triggers extend derived metrics in
derived.json — probes.py probes get an exact EDIT SUGGESTION, because
generated code is never silently rewritten.
"""
import json
import os


def run(a):
    from ..loop import apply_synonyms, propose_synonyms

    instance_dir = os.path.abspath(a.instance_dir)
    if getattr(a, "apply", None):
        summary = apply_synonyms(instance_dir, a.apply)
        print(json.dumps(summary, indent=2))
        return
    blob = propose_synonyms(instance_dir, audit_path=a.audit)
    print(json.dumps(blob, indent=2, ensure_ascii=False))
    n = len(blob["proposals"])
    print(f"\n{n} proposal(s) in {instance_dir}/synonym_proposals.json — "
          f"set \"approved\": true on the ones you accept, then:\n"
          f"  neuralosd synonyms --instance-dir {instance_dir} "
          f"--apply {instance_dir}/synonym_proposals.json")
