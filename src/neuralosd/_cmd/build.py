"""`neuralosd build` — natural language in, VERIFIED capability out.

    neuralosd build "average shipping cost per unit by region" \\
        --instance-dir .

Pi writes the probe code inside the instance directory. Neuralosd then
judges: banks untouched, instance loads, both banks replay green. Anything
else -> the instance is restored to its pre-pi state and the report says so.
A successful build still leaves the new probe SHADOWED conceptually: its
birth trap is banked, and nothing is trusted until the golden bank grows
around it.
"""
import json
import os
import sys


def run(a):
    from ..pisources import (DEFAULT_MODEL, DEFAULT_PROVIDER,
                             build_capability)
    question = " ".join(a.question or [])
    if not question:
        print("error: describe the capability to build", file=sys.stderr)
        return 2
    model = getattr(a, "model", None) or os.environ.get(
        "NEURALOSD_PI_MODEL") or DEFAULT_MODEL
    provider = getattr(a, "provider", None) or os.environ.get(
        "NEURALOSD_PI_PROVIDER") or DEFAULT_PROVIDER
    durable = getattr(a, "durable", False) or getattr(a, "resume", False)
    label = "DURABLE" + (" (resume)" if getattr(a, "resume", False)
                         else "") if durable else "one-shot"
    print(f"building with pi ({model}, {label}) inside "
          f"{os.path.abspath(a.instance_dir)} ...")
    print("  this runs a real agent turn — minutes, not seconds\n")
    if durable:
        from ..pisources import build_durable
        report = build_durable(question, instance_dir=a.instance_dir,
                               model=model, provider=provider,
                               timeout=getattr(a, "timeout", 900) or 900,
                               resume=getattr(a, "resume", False))
    else:
        report = build_capability(question, instance_dir=a.instance_dir,
                                  model=model, provider=provider,
                                  timeout=getattr(a, "timeout", 900) or 900,
                                  session=getattr(a, "session", None))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("storage"):
        print(f"\ndurable storage: {report['storage']}")
        print("crashed or interrupted? re-attach and continue:")
        print(f'  neuralosd build "{question}" --instance-dir '
              f'{a.instance_dir} --durable --resume')
    if report["ok"]:
        print("\n✓ verified: banks untouched, instance loads, both banks "
              "replay green.\n  the new probe has its misuse trap banked — "
              "grow the golden bank around it\n  (record oracles, run "
              "`neuralosd golden --dir .` in CI) before trusting it.")
        return 0
    print(f"\n✗ NOT DELIVERED: {report.get('error')}")
    if report.get("restored"):
        print("  the instance was restored to its pre-pi state — "
              "nothing broken was left behind")
    return 1
