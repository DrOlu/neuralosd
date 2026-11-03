"""`neuralosd browser` — agent-browser session passthrough (tier 2).

    neuralosd browser --session acme open https://portal.corp
    neuralosd browser --session acme snapshot
    neuralosd browser --session acme click e12
    neuralosd browser --session acme fill e7 "search text"
    neuralosd browser --session acme network

All ops pin the named session, so login state and SPA navigation survive
across probes. The underlying agent-browser CLI must be installed.
"""
import json
import sys


def run(a):
    op = getattr(a, "op", None)
    if op == "setup" or getattr(a, "setup", False):
        from ..websources import setup_browser_tool
        report = setup_browser_tool(assume_yes=bool(getattr(a, "yes", False)))
        print(json.dumps(report, indent=2))
        if report.get("already"):
            print("\nagent-browser was already installed — verified with a "
                  "live snapshot ping")
        elif report["ping_ok"]:
            print("\n✓ agent-browser installed and verified — "
                  "neuralosd init --source browser:<url> is ready")
        else:
            print(f"\n✗ setup incomplete: {report.get('error') or 'unknown'}")
            raise SystemExit(1)
        return

    from ..websources import browser_session_op
    out = browser_session_op(session=a.session, op=a.op, url=a.url,
                             target=a.target, text=a.text)
    if a.op == "snapshot":
        print(out)
        return
    print(json.dumps({"ok": True, "op": a.op, "out": (out or "")[:2000]},
                     indent=2))
