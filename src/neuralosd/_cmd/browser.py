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


def run(a):
    from ..websources import browser_session_op
    out = browser_session_op(session=a.session, op=a.op, url=a.url,
                             target=a.target, text=a.text)
    if a.op == "snapshot":
        print(out)
        return
    print(json.dumps({"ok": True, "op": a.op, "out": (out or "")[:2000]},
                     indent=2))
