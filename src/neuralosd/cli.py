"""neuralosd CLI — the `neuralosd` command.

Subcommands:
  init       — scaffold a new instance from a data source
  deploy     — deploy an instance into a sandbox (boxlite or msb)
  ask        — ask a question
  serve      — start the HTTP service
  lint       — trigger collision linter
  golden     — run the golden question bank
  truth      — run the truth oracle (database instances)
  invariants — run property checks
  calibrate  — learn per-probe confidence gates
  gaps       — mine gated/fuzzy questions for menu improvements
  openapi    — generate OpenAPI 3.1 + MCP manifest from the menu
  diff       — profile/instance schema-evolution diff
  backends   — list available sandbox backends
"""
import argparse
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser(
        prog="neuralosd",
        description="Deterministic-first agentic runtime for neuralOS")
    sub = ap.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Scaffold a new instance from a data source")
    p_init.add_argument("--source", required=True, help="Data source (CSV, DB DSN, JSON, log, API URL)")
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--out", help="Output dir (default: ./<name>)")
    p_init.add_argument("--packages", default="", help="Extra pip packages")

    # deploy
    p_deploy = sub.add_parser("deploy", help="Deploy into a sandbox")
    p_deploy.add_argument("--instance-dir", required=True)
    p_deploy.add_argument("--name", required=True)
    p_deploy.add_argument("--backend", choices=["boxlite", "msb"], default="boxlite")
    p_deploy.add_argument("--port", type=int, default=8877)
    p_deploy.add_argument("--packages", default="pydantic")

    # ask
    p_ask = sub.add_parser("ask", help="Ask a question")
    p_ask.add_argument("--instance-dir", required=True)
    p_ask.add_argument("question", nargs="+")

    # serve
    p_serve = sub.add_parser("serve", help="Start the HTTP service")
    p_serve.add_argument("--instance-dir", required=True)
    p_serve.add_argument("--port", type=int, default=8877)

    # lint
    p_lint = sub.add_parser("lint", help="Trigger collision linter")
    p_lint.add_argument("menu", help="needle_menu.json path")
    p_lint.add_argument("--top", type=int, default=8)

    # golden
    p_golden = sub.add_parser("golden", help="Run golden question bank")
    p_golden.add_argument("--dir", default=".", help="Instance dir")

    # truth
    p_truth = sub.add_parser("truth", help="Run truth oracle")
    p_truth.add_argument("--dir", default=".", help="Instance dir")

    # invariants
    p_inv = sub.add_parser("invariants", help="Run property checks")
    p_inv.add_argument("--dir", default=".", help="Instance dir")

    # calibrate
    p_cal = sub.add_parser("calibrate", help="Learn per-probe confidence gates")
    p_cal.add_argument("--golden", default="golden.json")
    p_cal.add_argument("--base", default="http://127.0.0.1:8877")
    p_cal.add_argument("--out", default="calibration.json")

    # gaps
    p_gaps = sub.add_parser("gaps", help="Mine gated/fuzzy questions")
    p_gaps.add_argument("log", help="ask_audit.jsonl or menu_gaps.jsonl")
    p_gaps.add_argument("--menu", default="needle_menu.json")
    p_gaps.add_argument("--out", default="proposals.json")

    # openapi
    p_oa = sub.add_parser("openapi", help="Generate OpenAPI + MCP manifest")
    p_oa.add_argument("menu", help="needle_menu.json path")
    p_oa.add_argument("--agent", default="neuralos-instance")

    # diff
    p_diff = sub.add_parser("diff", help="Profile schema-evolution diff")
    p_diff.add_argument("old")
    p_diff.add_argument("new")
    p_diff.add_argument("--menu", default=None)

    # backends
    p_docs = sub.add_parser("docs", help="Read bundled documentation")
    p_docs.add_argument("topic", nargs="?", default=None)
    sub.add_parser("backends", help="List available sandbox backends")

    a = ap.parse_args()
    if not a.command:
        ap.print_help()
        return

    if a.command == "init":
        from . import _cmd_init
        _cmd_init.run(a)
    elif a.command == "deploy":
        from . import _cmd_deploy
        _cmd_deploy.run(a)
    elif a.command == "ask":
        from . import _cmd_ask
        _cmd_ask.run(a)
    elif a.command == "serve":
        from . import _cmd_serve
        _cmd_serve.run(a)
    elif a.command == "lint":
        from . import _cmd_lint
        _cmd_lint.run(a)
    elif a.command == "golden":
        from . import _cmd_golden
        _cmd_golden.run(a)
    elif a.command == "truth":
        from . import _cmd_truth
        _cmd_truth.run(a)
    elif a.command == "invariants":
        from . import _cmd_invariants
        _cmd_invariants.run(a)
    elif a.command == "calibrate":
        from . import _cmd_calibrate
        _cmd_calibrate.run(a)
    elif a.command == "gaps":
        from . import _cmd_gaps
        _cmd_gaps.run(a)
    elif a.command == "openapi":
        from . import _cmd_openapi
        _cmd_openapi.run(a)
    elif a.command == "diff":
        from . import _cmd_diff
        _cmd_diff.run(a)
    elif a.command == "docs":
        import neuralosd as _mod
        print(_mod.docs(getattr(a, "topic", None)))
    elif a.command == "backends":
        print("Available backends:")
        print("  boxlite — persistent microVM boxes (pip install boxlite)")
        print("  msb     — Microsandbox sandboxes (msb CLI)")
        print("\nBoth use the same probe contract. Switch by configuration.")


if __name__ == "__main__":
    main()
