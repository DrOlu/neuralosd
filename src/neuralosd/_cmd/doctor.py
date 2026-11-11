"""`neuralosd doctor` — one command that health-checks every integration.

    neuralosd doctor --instance-dir .

Checks pi, Pi Durable (node + package), agent-browser, Temporal (CLI +
SDK), the synapse gateway token, data libs (openpyxl/sqlite3), and the
instance's own banks. JSON or human output; exit 1 when anything fails.
"""
import json
import os
import sys


def _checks(instance_dir):
    from .. import pisources, pidurable, websources, durable
    out = {}

    # pi
    try:
        r = pisources.setup(model=pisources.DEFAULT_MODEL,
                            provider=pisources.DEFAULT_PROVIDER)
        out["pi"] = {"ok": bool(r.get("ping_ok")),
                     "detail": (f"ready ({r.get('model')})"
                                if r.get("ping_ok")
                                else r.get("error") or "ping failed")}
    except SystemExit as e:
        out["pi"] = {"ok": False, "detail": str(e)[:140]}

    # pi durable
    if not pidurable.node_available():
        out["pi_durable"] = {"ok": False, "detail": "node not installed"}
    elif not pidurable.pi_durable_available():
        out["pi_durable"] = {"ok": False,
                             "detail": "npm i @earendil-works/pi-durable "
                                       "@earendil-works/pi-ai"}
    else:
        out["pi_durable"] = {"ok": True, "detail": "node + package ready"}

    # agent-browser
    ab = websources._execute if False else None
    import shutil
    out["agent_browser"] = {
        "ok": shutil.which("agent-browser") is not None,
        "detail": ("installed" if shutil.which("agent-browser")
                   else "npm i -g agent-browser (only needed for browser "
                        "sources)")}

    # temporal
    cli = durable._which("temporal")
    sdk = durable.temporalio_available()
    out["temporal"] = {
        "ok": bool(cli and sdk),
        "detail": ("CLI + SDK ready" if cli and sdk else
                   ", ".join(filter(None, [
                       "CLI missing" if not cli else "",
                       "SDK missing (pip install temporalio)"
                       if not sdk else ""])))}

    # synapse token
    from ..synapse import SynapseHTTP
    tok = SynapseHTTP._resolve_token(None)
    out["synapse_token"] = {
        "ok": tok is not None,
        "detail": ("resolved" if tok else
                   "set REACTORPRO_GATEWAY_TOKEN or gateway.env "
                   "(only needed for mesh ops)")}

    # data libs
    try:
        import openpyxl                                   # noqa: F401
        out["openpyxl"] = {"ok": True, "detail": "xlsx sources ready"}
    except ImportError:
        out["openpyxl"] = {"ok": False,
                           "detail": "pip install openpyxl (xlsx sources)"}

    # the instance's own banks
    try:
        from ._common import load_instance
        inst = load_instance(instance_dir)
        golden_path = os.path.join(instance_dir, "golden.json")
        traps_path = os.path.join(instance_dir, "traps.json")
        if os.path.isfile(golden_path):
            with open(golden_path, encoding="utf-8") as fh:
                bank = json.load(fh)
        else:
            bank = {"items": []}
        if os.path.isfile(traps_path):
            with open(traps_path, encoding="utf-8") as fh:
                t = json.load(fh)
            bank["items"].extend(t.get("items", []))
        summary = inst.golden_run(bank)
        ok = summary["wrong"] == 0 and summary["answered_traps"] == 0
        out["banks"] = {"ok": ok,
                        "detail": f"{len(bank.get('items', []))} cases, "
                                  f"{summary['wrong']} wrong, "
                                  f"{summary['answered_traps']} traps "
                                  f"answered"}
    except SystemExit as e:
        out["banks"] = {"ok": False, "detail": str(e)[:140]}
    return out


def run(a):
    from ..pisources import setup as _unused  # noqa: F401  (warm import)
    checks = _checks(os.path.abspath(a.instance_dir))
    ok = all(v.get("ok") for v in checks.values())
    if getattr(a, "as_json", False):
        print(json.dumps(checks, indent=2))
    else:
        for name, v in checks.items():
            mark = "✓" if v.get("ok") else "✗"
            print(f"  {mark} {name:<16} {v.get('detail', '')}")
        print(f"\n{'all checks passed' if ok else str(sum(1 for v in checks.values() if not v.get('ok'))) + ' check(s) failed'}")
    return 0 if ok else 1
