"""`neuralosd badge` — golden-bank health as a badge + CI gate.

    neuralosd badge --instance-dir . --out badge.json \
        --actions-out .github/workflows/golden.yml

The badge JSON is a shields.io endpoint: commit it, point a shield at the
raw URL, and the README shows the bank's health. The Actions snippet gates
every push on the bank.
"""
import json
import os


def run(a):
    from ..loop import write_badge
    badge = write_badge(os.path.abspath(a.instance_dir), a.out,
                        actions_out=a.actions_out)
    print(json.dumps(badge, indent=2))
    if a.actions_out:
        print(f"workflow -> {a.actions_out}")
    print(f"\nshields endpoint (commit {a.out}, then):")
    print(f"  https://img.shields.io/endpoint?url="
          f"<raw-url-to-{os.path.basename(a.out)}>")
