"""`neuralosd pi` — the pi coding agent as operator (and setup).

    neuralosd pi --setup                          # pi + model + live ping
    neuralosd pi ask "why did refusals go up?"    # operator, closed commands

The builder lives at `neuralosd build "<capability>"` — same runner, plus
the verification/restore pipeline.
"""
import json
import os
import sys


def run(a):
    from ..pisources import (DEFAULT_MODEL, DEFAULT_PROVIDER, operate,
                             setup)
    model = getattr(a, "model", None) or os.environ.get(
        "NEURALOSD_PI_MODEL") or DEFAULT_MODEL
    provider = getattr(a, "provider", None) or os.environ.get(
        "NEURALOSD_PI_PROVIDER") or DEFAULT_PROVIDER

    if getattr(a, "setup", False):
        try:
            report = setup(model=model, provider=provider)
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        if report["ping_ok"]:
            print(f"\n✓ pi is ready ({model}) — build capabilities with:\n"
                  f"  neuralosd build \"<capability description>\" "
                  f"--instance-dir .")
            return 0
        print(f"\n✗ {report.get('error')}")
        return 1

    if getattr(a, "ask", None):
        question = " ".join(a.ask)
        answer = operate(question, instance_dir=os.path.abspath(
            a.instance_dir), model=model, provider=provider,
            timeout=getattr(a, "timeout", 900) or 900,
            session=getattr(a, "session", None))
        print(answer)
        print("\n(pi's answer is UNTRUSTED ASSISTANT OUTPUT — verify before "
              "acting on it)")
        return 0

    print("nothing to do: use --setup or ask \"<question>\"", file=sys.stderr)
    return 2
