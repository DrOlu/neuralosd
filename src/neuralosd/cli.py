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
  scrub      — sanitize cache + audit records already on disk
  reason     — escalate to a reasoning model, then extend the menu
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
    p_ask.add_argument("--model", action="store_true",
                       help="Load the on-device model as a fallback when no "
                            "probe matches (slower startup)")
    p_ask.add_argument("--strict", action="store_true",
                       help="Refuse when the answer would have dropped a "
                            "filter named in the question, instead of "
                            "returning it with a `discarded` ledger")
    p_ask.add_argument("question", nargs="+")

    # serve
    p_serve = sub.add_parser("serve", help="Start the HTTP service")
    p_serve.add_argument("--instance-dir", required=True)
    p_serve.add_argument("--port", type=int, default=8877)
    p_serve.add_argument("--model", action="store_true",
                         help="Load the on-device model as a fallback")
    p_serve.add_argument("--strict", action="store_true",
                         help="Refuse answers that would drop a filter")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="Bind address (default 127.0.0.1; use 0.0.0.0 "
                              "to expose — there is NO authentication)")

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

    # reason
    p_rs = sub.add_parser(
        "reason",
        help="Escalate a question to a reasoning model, then extend the menu")
    p_rs.add_argument("--instance-dir", required=True)
    p_rs.add_argument("question", nargs="+")
    p_rs.add_argument("--model", default=None,
                      help="Ollama model (default deepseek-r1:8b)")
    p_rs.add_argument("--ollama", default=None,
                      help="Ollama base URL (default http://127.0.0.1:11434)")
    p_rs.add_argument("--oracle", type=float, default=None,
                      help="an independently-computed expected RATIO; the "
                           "loop never sees where it came from")
    p_rs.add_argument("--scale", choices=["ratio", "percent"],
                      default="percent")
    p_rs.add_argument("--dry-run", action="store_true",
                      help="show the whole trail, write nothing")
    p_rs.add_argument("--force", action="store_true",
                      help="escalate even when the gate says not to")
    p_rs.add_argument("--no-stability-check", action="store_true",
                      help="ask the model once instead of twice (faster, and "
                           "less safe)")
    p_rs.add_argument("--timeout", type=float, default=240.0)

    # scrub
    p_scrub = sub.add_parser(
        "scrub", help="Sanitize cache + audit records already on disk")
    p_scrub.add_argument("--instance-dir", required=True)
    p_scrub.add_argument("--dry-run", action="store_true", help="report only")
    p_scrub.add_argument("--purge-cache", action="store_true",
                         help="delete the cache instead of rewriting it")

    # sidecar
    p_sc = sub.add_parser(
        "sidecar",
        help="Provision a sidecar environment (lets this binary use host libraries)")
    p_sc.add_argument("--status", action="store_true", help="report and exit")
    p_sc.add_argument("--setup", action="store_true", help="create the environment")
    p_sc.add_argument("--with", dest="packages", default="",
                      help="extra packages, comma separated (e.g. pypdf,pywinrm)")
    p_sc.add_argument("--python", default="3.12")
    p_sc.add_argument("--force", action="store_true", help="rebuild from scratch")

    a = ap.parse_args()
    if not a.command:
        ap.print_help()
        return

    if a.command == "init":
        from ._cmd import init
        init.run(a)
    elif a.command == "deploy":
        from ._cmd import deploy
        deploy.run(a)
    elif a.command == "ask":
        from ._cmd import ask
        ask.run(a)
    elif a.command == "serve":
        from ._cmd import serve
        serve.run(a)
    elif a.command == "lint":
        from ._cmd import lint
        lint.run(a)
    elif a.command == "golden":
        from ._cmd import golden
        golden.run(a)
    elif a.command == "truth":
        from ._cmd import truth
        truth.run(a)
    elif a.command == "invariants":
        from ._cmd import invariants
        invariants.run(a)
    elif a.command == "calibrate":
        from ._cmd import calibrate
        calibrate.run(a)
    elif a.command == "gaps":
        from ._cmd import gaps
        gaps.run(a)
    elif a.command == "openapi":
        from ._cmd import openapi
        openapi.run(a)
    elif a.command == "diff":
        from ._cmd import diff
        diff.run(a)
    elif a.command == "scrub":
        from ._cmd import scrub
        raise SystemExit(scrub.run(a))
    elif a.command == "reason":
        from ._cmd import reason
        raise SystemExit(reason.run(a))
    elif a.command == "docs":
        import neuralosd as _mod
        print(_mod.docs(getattr(a, "topic", None)))
    elif a.command == "sidecar":
        from .provision import main as sidecar_main
        raise SystemExit(sidecar_main(
            (["--setup"] if a.setup else [])
            + (["--status"] if a.status else [])
            + ["--with", a.packages, "--python", a.python]
            + (["--force"] if a.force else [])))
    elif a.command == "backends":
        from .backends import available_backends
        av = available_backends()
        print("Sandbox backends:")
        for _name in ("boxlite", "msb"):
            _ok = av.get(_name, False)
            _mark = "available" if _ok else "not installed"
            print(f"  {_name:<8} [{_mark}]")
        print("\nBoth use the same probe contract. Switch by configuration.")
        if not any(av.values()):
            print("\nInstall one:  pip install 'neuralosd[boxlite]'  or  "
                  "pip install 'neuralosd[msb]'")


if __name__ == "__main__":
    main()
