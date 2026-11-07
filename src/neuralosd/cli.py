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


def _gossip_blob(a):
    """The fingerprint blob to publish: a pre-exported file, or mined fresh
    from the audit log."""
    import os as _os
    if getattr(a, "fingerprints", None) and _os.path.isfile(a.fingerprints):
        with open(a.fingerprints, encoding="utf-8") as fh:
            return json.load(fh)
    if getattr(a, "audit", None) and _os.path.isfile(a.audit):
        from neuralosd.loop import cluster_gaps, export_fingerprints
        gaps = []
        with open(a.audit, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("results") in (None, [], {}):
                    gaps.append({"question": r.get("question")})
        return export_fingerprints(cluster_gaps(gaps))
    print("error: gossip-push needs --fingerprints or --audit",
          file=sys.stderr)
    sys.exit(2)


def _gossip_transport(a):
    from neuralosd.synapse import SynapseHTTP
    return SynapseHTTP(gateway=getattr(a, "gateway", None)
                       or "http://127.0.0.1:3000",
                       timeout=getattr(a, "timeout", 30))


def _harden_streams():
    """Never let a console encoding turn an answer (or a doc page) into a crash.

    Windows consoles default to cp1252 and POSIX C locales to ASCII, so
    `print()` of the shipped documentation raised

        UnicodeEncodeError: 'charmap' codec can't encode characters …

    and `neuralosd docs usage` — the command a new user runs first — exited 1
    with a traceback. Setting errors="replace" keeps every ASCII character
    readable and degrades only the glyphs the console cannot draw; streams that
    are already UTF-8 (macOS, Linux, Windows Terminal) are untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", None) or "").lower()
            if enc.replace("-", "") in ("utf8", "utf8mb4", "cp65001"):
                continue
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main():
    _harden_streams()
    ap = argparse.ArgumentParser(
        prog="neuralosd",
        description="Deterministic-first agentic runtime for neuralOS")
    sub = ap.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Scaffold a new instance from a data source")
    p_init.add_argument("--extra-source", action="append", default=None,
                        help="additional delimited source (repeatable)")
    p_init.add_argument("--on", dest="join_on", default=None,
                        help="join key between main and extra source: "
                             "leftcol=rightcol")
    p_init.add_argument("--since", default=None,
                        help="journal window, e.g. 1h (journal sources)")
    p_init.add_argument("--header", default=None,
                        help="header name carrying the credential (api)")
    p_init.add_argument("--secret-ref", default=None,
                        help="credential REFERENCE: env:VAR | scrt:<store>:<key>"
                             " | op:<vault>/<item>/<field> — never the secret")
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
    p_deploy.add_argument("--bind", default="127.0.0.1",
                          help="host bind address for the forwarded door")
    p_deploy.add_argument("--force", action="store_true",
                          help="remove and redeploy an existing sandbox")
    p_deploy.add_argument("--index-url", default=None,
                          help="extra pip index for the in-sandbox install")
    p_deploy.add_argument("--from-template", default=None,
                          help="fork the sandbox from a baked template "
                               "(msb: snapshot file; boxlite: box name) "
                               "instead of create+pip-install")
    p_deploy.add_argument("--save-template", default=None,
                          help="after a verified deploy, bake this sandbox "
                               "into a template for future --from-template "
                               "deploys (msb: snapshot file)")

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
    p_ask.add_argument("--as-of", default=None, metavar="LABEL",
                       help="answer from a pinned snapshot "
                            "(neuralosd snapshot --label LABEL)")
    p_ask.add_argument("--role", default=None,
                       help="answer AS this role (acl.json controls the "
                            "probe allow-list and row filters)")
    p_ask.add_argument("question", nargs="+")

    # serve
    p_serve = sub.add_parser("serve", help="Start the HTTP service")
    p_serve.add_argument("--instance-dir", default=None,
                         help="single instance directory")
    p_serve.add_argument("--instances-dir", default=None,
                         help="directory containing MULTIPLE instance dirs; "
                              "each is served at /{name}/ask")
    p_serve.add_argument("--port", type=int, default=8877)
    p_serve.add_argument("--model", action="store_true",
                         help="Load the on-device model as a fallback")
    p_serve.add_argument("--strict", action="store_true",
                         help="Refuse answers that would drop a filter")
    p_serve.add_argument("--auth-token",
                         help="require Authorization: Bearer <token> on every "
                              "endpoint except /healthz and /ready (or set "
                              "$NEURALOSD_API_TOKEN)")
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
    p_golden.add_argument("--record", action="store_true",
                          help="append a case WITH its oracle receipt")
    p_golden.add_argument("--receipts", action="store_true",
                          help="print the evidence chain (who/when/source)")
    p_golden.add_argument("--question", nargs="+", default=None)
    p_golden.add_argument("--expect-probe", default=None)
    p_golden.add_argument("--expect-refusal", action="store_true")
    p_golden.add_argument("--value", default=None,
                          help="the oracle value being attested")
    p_golden.add_argument("--by", default=None, help="who verified it")
    p_golden.add_argument("--source", default=None,
                          help="where the value came from (invoice, ledger...)")
    p_golden.add_argument("--method", default=None,
                          help="how it was verified (manual count, SQL...)")

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
    p_cal.add_argument("--traps", default=None,
                       help="trap bank JSON (default <instance>/traps.json)")
    p_cal.add_argument("--allow-no-traps", action="store_true",
                       help="fit without a trap bank (not recommended)")

    # gaps
    p_gaps = sub.add_parser("gaps", help="Mine gated/fuzzy questions")
    p_gaps.add_argument("log", help="ask_audit.jsonl or menu_gaps.jsonl")
    p_gaps.add_argument("--menu", default="needle_menu.json")
    p_gaps.add_argument("--out", default="proposals.json")
    p_gaps.add_argument("--export-fingerprints", default=None,
                        help="export anonymized gap fingerprints for the mesh")
    p_gaps.add_argument("--ingest-fingerprints", default=None,
                        help="merge a peer's fingerprints into the report")

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

    # mesh
    # feedback — thumbs-down -> bank candidates (v2.3.0)
    p_fb = sub.add_parser(
        "feedback",
        help="Turn a rejected answer into a golden/trap candidate")
    p_fb.add_argument("--instance-dir", required=True)
    p_fb.add_argument("--question", nargs="+", default=[])
    p_fb.add_argument("--verdict", choices=["bad", "invalid"], default=None)
    p_fb.add_argument("--correction", default=None,
                      help="the user's corrected question")
    p_fb.add_argument("--by", default=None, help="who reported it")
    p_fb.add_argument("--apply", action="store_true",
                      help="promote proven candidates into the banks")

    # mothball — menu hygiene (v2.3.0)
    p_mb = sub.add_parser(
        "mothball",
        help="Demote probes nobody queries (kept, reinstatable)")
    p_mb.add_argument("--instance-dir", required=True)
    p_mb.add_argument("--age-days", type=int, default=180)
    p_mb.add_argument("--apply", action="store_true")
    p_mb.add_argument("--reinstate", default=None, metavar="PROBE")

    # drift — schema-drift-triggered re-audit (v2.3.0)
    p_dr = sub.add_parser(
        "drift",
        help="Re-audit the instance against a changed source schema")
    p_dr.add_argument("--instance-dir", required=True)
    p_dr.add_argument("--source", required=True,
                      help="the NEW source (file/db) to re-profile")

    # transfer — schema-similarity probe transfer (v2.3.0)
    p_tr = sub.add_parser(
        "transfer",
        help="Propose a schema-sibling's probes + translated golden bank")
    p_tr.add_argument("--from", dest="source", required=True)
    p_tr.add_argument("--to", dest="target", required=True)
    p_tr.add_argument("--min-sim", type=float, default=0.6)
    p_tr.add_argument("--out", default=None)

    # shadow — shadow mode for generated probes (v2.3.0)
    p_sh = sub.add_parser(
        "shadow",
        help="Report / evaluate / promote shadowed probes")
    p_sh.add_argument("--instance-dir", required=True)
    p_sh.add_argument("--evaluate", action="store_true",
                      help="run one agreement pass against the oracles")
    p_sh.add_argument("--promote", action="store_true",
                      help="promote shadowed probes that qualify")
    p_sh.add_argument("--min-agreement", type=float, default=0.9)
    p_sh.add_argument("--min-runs", type=int, default=3)
    p_sh.add_argument("--max-age-days", type=int, default=14)

    # browser — agent-browser session passthrough (v2.4.0)
    p_br = sub.add_parser(
        "browser",
        help="Drive a named agent-browser session (open/snapshot/click/fill)")
    p_br.add_argument("--session", default=None)
    p_br.add_argument("op", nargs="?",
                      choices=["open", "snapshot", "click", "fill",
                               "type", "network", "text", "setup"])
    p_br.add_argument("--url", default=None)
    p_br.add_argument("--target", default=None, help="ref or selector")
    p_br.add_argument("--text", default=None)
    p_br.add_argument("--setup", action="store_true",
                      help="check Node, install the agent-browser CLI via "
                           "npm (WITH consent), and verify with a live ping")
    p_br.add_argument("--yes", action="store_true",
                      help="skip the consent prompt (non-interactive)")

    # snapshot — pin hashed observations (v2.4.0)
    p_snap = sub.add_parser(
        "snapshot", help="Pin a hashed snapshot of every probe's observation")
    p_snap.add_argument("--instance-dir", required=True)
    p_snap.add_argument("--label", required=True)

    # slo — refusal-rate SLO (v2.4.0)
    p_slo = sub.add_parser(
        "slo", help="Refusal-rate SLO for one instance")
    p_slo.add_argument("--instance-dir", required=True)
    p_slo.add_argument("--window-hours", type=int, default=24)
    p_slo.add_argument("--max-refusal-rate", type=float, default=0.4)

    # fleet — multi-instance dashboard (v2.4.0)
    p_fl = sub.add_parser(
        "fleet", help="Multi-instance health dashboard")
    p_fl.add_argument("dirs", nargs="+", help="instance dirs (globs ok)")
    p_fl.add_argument("--window-hours", type=int, default=24)
    p_fl.add_argument("--max-refusal-rate", type=float, default=0.4)

    # badge — golden-bank shields + CI gate (v2.4.0)
    p_bd = sub.add_parser(
        "badge", help="Golden-bank health badge + GitHub Actions snippet")
    p_bd.add_argument("--instance-dir", required=True)
    p_bd.add_argument("--out", default="badge.json")
    p_bd.add_argument("--actions-out", default=None)

    # synonyms — cluster-driven trigger proposals (v2.4.0)
    p_syn = sub.add_parser(
        "synonyms",
        help="Propose (and, when approved, apply) triggers from gap clusters")
    p_syn.add_argument("--instance-dir", required=True)
    p_syn.add_argument("--audit", default=None)
    p_syn.add_argument("--apply", metavar="PROPOSALS_JSON", default=None)

    # actuate — named, journaled, reversible actions (v2.9.0)
    p_act = sub.add_parser(
        "actuate",
        help="Named, journaled, reversible action (dry-run -> apply)")
    p_act.add_argument("--instance-dir", required=True)
    p_act.add_argument("--action", required=True,
                       help="block_ip | isolate_host | disable_user | "
                            "rotate_credential | quarantine_file")
    p_act.add_argument("--target", required=True)
    p_act.add_argument("--reason", default=None,
                       help="WHY — required")
    p_act.add_argument("--operator", default=None,
                       help="who is doing this")
    p_act.add_argument("--dry-run", action="store_true")
    p_act.add_argument("--apply", action="store_true",
                       help="fire the action (needs --plan from a dry-run)")
    p_act.add_argument("--plan", default=None,
                       help="the plan_id from a previous dry-run")
    p_act.add_argument("--undo", action="store_true",
                       help="reverse a previously applied action")
    p_act.add_argument("--undo-plan", default=None, metavar="PLAN_ID")

    # watch — streaming ingestion (v2.8.1)
    p_w = sub.add_parser(
        "watch", help="Stream sources into a bounded buffer; alert on rules")
    p_w.add_argument("--instance-dir", required=True)
    p_w.add_argument("--source", action="append", default=None,
                     help="syslog:/path | journal (repeatable)")
    p_w.add_argument("--rules", default=None, help="rules JSON file")
    p_w.add_argument("--once", action="store_true",
                     help="one poll cycle, then exit")
    p_w.add_argument("--interval", type=int, default=30)
    p_w.add_argument("--cap", type=int, default=10000)
    p_w.add_argument("--webhook", default=None)

    # roles — RBAC management (v2.8.0)
    p_rl = sub.add_parser(
        "roles", help="Manage acl.json roles (allow-lists + row filters)")
    p_rl.add_argument("--instance-dir", required=True)
    p_rl.add_argument("--op", choices=["list", "set", "show", "remove"],
                      default="list")
    p_rl.add_argument("--role", default=None)
    p_rl.add_argument("--allow", default=None,
                      help="comma-separated probe names")
    p_rl.add_argument("--row-filter", default=None,
                      help="'column=in:value1,value2' — rows served to "
                           "this role are filtered to these values")
    p_rl.add_argument("--json", dest="as_json", action="store_true")

    # doctor — environment health check (v2.7.0)
    p_doc = sub.add_parser(
        "doctor", help="Health-check every integration (pi, durable, "
                       "browser, temporal, synapse, banks)")
    p_doc.add_argument("--instance-dir", default=".")
    p_doc.add_argument("--json", dest="as_json", action="store_true")

    # temporal — durable execution (v2.5.0)
    p_tm = sub.add_parser(
        "temporal", help="Durable execution of the loop (Temporal)")
    p_tm.add_argument("--setup", action="store_true",
                      help="check CLI + server + temporalio SDK")
    p_tm.add_argument("--run-activity", default=None, metavar="NAME",
                      help="run one loop activity directly (no server)")
    p_tm.add_argument("--worker", action="store_true",
                      help="run a durable worker for this instance")
    p_tm.add_argument("--schedule-create", action="store_true",
                      help="create a recurring loop schedule (Temporal CLI)")
    p_tm.add_argument("--instance-dir", default=".")
    p_tm.add_argument("--task-queue", default="neuralos")
    p_tm.add_argument("--cron", default="0 2 * * *")
    p_tm.add_argument("--schedule-id", default="neuralos-nightly")
    p_tm.add_argument("--workflow-type", default="neuralos_loop")
    p_tm.add_argument("--source", default=None,
                      help="source for the drift_audit activity")

    # pi — the coding agent as builder/operator (v2.6.0)
    p_pi = sub.add_parser(
        "pi", help="Drive the pi coding agent (operator mode + setup)")
    p_pi.add_argument("--setup", action="store_true",
                      help="check pi, the model, and a live one-shot ping")
    p_pi.add_argument("ask", nargs="*", help="operator question")
    p_pi.add_argument("--instance-dir", default=".")
    p_pi.add_argument("--model", default=None,
                      help="default z-ai/glm-5.3-flash (openrouter) — or "
                           "$NEURALOSD_PI_MODEL")
    p_pi.add_argument("--provider", default=None,
                      help="default openrouter — or $NEURALOSD_PI_PROVIDER")
    p_pi.add_argument("--session", default=None)
    p_pi.add_argument("--timeout", type=int, default=900)
    p_pi.add_argument("--durable", action="store_true",
                      help="operator conversation in Pi Durable storage")
    p_pi.add_argument("--status", action="store_true",
                      help="durable: conversation status")
    p_pi.add_argument("--steer", default=None, metavar="MESSAGE",
                      help="durable: send a steering message")
    p_pi.add_argument("--stop", action="store_true",
                      help="durable: close the bridge cleanly")

    p_build = sub.add_parser(
        "build",
        help="Natural language in, VERIFIED capability out (pi + banks)")
    p_build.add_argument("question", nargs="+",
                         help="the capability to build, in plain language")
    p_build.add_argument("--instance-dir", required=True)
    p_build.add_argument("--model", default=None)
    p_build.add_argument("--provider", default=None)
    p_build.add_argument("--session", default=None)
    p_build.add_argument("--timeout", type=int, default=900)
    p_build.add_argument("--durable", action="store_true",
                         help="run the build in a Pi Durable conversation "
                              "(storage-backed, crash-resumable, exactly-"
                              "once submissions)")
    p_build.add_argument("--resume", action="store_true",
                         help="re-attach to the durable conversation and "
                              "continue (the reasoning survives crashes)")

    p_mesh = sub.add_parser(
        "mesh", help="Synapse mesh integration: serve, discover, delegate")
    mesh_sub = p_mesh.add_subparsers(dest="mesh_cmd")
    p_ms = mesh_sub.add_parser("serve", help="Serve this instance on the mesh")
    p_ms.add_argument("--instance-dir", required=True)
    p_ms.add_argument("--gateway", default="http://127.0.0.1:8090")
    p_ms.add_argument("--timeout", type=float, default=30)
    p_ms.add_argument("--publish", action="store_true",
                      help="publish the registration to the gateway and "
                           "re-assert it on every heartbeat")
    p_ms.add_argument("--heartbeat", type=float, default=30.0,
                      help="heartbeat interval in seconds")
    p_md = mesh_sub.add_parser("discover", help="Discover peers on the mesh")
    p_md.add_argument("--gateway", default=None)
    p_md.add_argument("--skill", default="", help="skill to search for")
    p_md.add_argument("--instance-name", default="")
    p_md.add_argument("--preflight", action="store_true",
                      help="ask each shortlisted peer would-you-answer and "
                           "keep only peers whose gate says yes")
    p_md.add_argument("--json", action="store_true")
    p_md.add_argument("--timeout", type=float, default=30)
    p_ms2 = mesh_sub.add_parser("agents", help="The peer directory (phonebook)")
    p_ms2.add_argument("--gateway", default=None)
    p_ms2.add_argument("--capabilities", default="")
    p_ms2.add_argument("--timeout", type=float, default=30)
    p_msend = mesh_sub.add_parser(
        "send", help="Ask a peer synchronously (dispatch / invoke)")
    p_msend.add_argument("--gateway", default=None)
    p_msend.add_argument("--target", required=True)
    p_msend.add_argument("question", nargs="+")
    p_msend.add_argument("--timeout-ms", type=int, default=120000)
    p_msend.add_argument("--timeout", type=float, default=180)
    p_mtc = mesh_sub.add_parser(
        "task-create", help="Durable async task on a peer (idempotent)")
    p_mtc.add_argument("--gateway", default=None)
    p_mtc.add_argument("--target", required=True)
    p_mtc.add_argument("question", nargs="+")
    p_mtc.add_argument("--task-id", default=None,
                       help="caller-minted idempotency key")
    p_mtc.add_argument("--timeout", type=float, default=30)
    p_mts = mesh_sub.add_parser(
        "task-status", help="Task state (--refresh asks the owning edge)")
    p_mts.add_argument("--gateway", default=None)
    p_mts.add_argument("task_id")
    p_mts.add_argument("--refresh", action="store_true")
    p_mts.add_argument("--timeout", type=float, default=30)
    p_mtx = mesh_sub.add_parser("task-cancel", help="Cancel a task")
    p_mtx.add_argument("--gateway", default=None)
    p_mtx.add_argument("task_id")
    p_mtx.add_argument("--timeout", type=float, default=30)
    p_mtr = mesh_sub.add_parser(
        "task-retry", help="Re-run a failed/canceled task under the same id")
    p_mtr.add_argument("--gateway", default=None)
    p_mtr.add_argument("task_id")
    p_mtr.add_argument("--timeout", type=float, default=30)
    p_mmb = mesh_sub.add_parser(
        "mailbox", help="Durable mailbox message (fire-and-forget)")
    p_mmb.add_argument("--gateway", default=None)
    p_mmb.add_argument("--target", required=True)
    p_mmb.add_argument("--skill", default="status")
    p_mmb.add_argument("--timeout", type=float, default=30)
    p_mtr2 = mesh_sub.add_parser("trust", help="Pinned peer identities")
    p_mtr2.add_argument("--gateway", default=None)
    p_mtr2.add_argument("--timeout", type=float, default=30)
    p_msetup = mesh_sub.add_parser(
        "setup", help="Check the gateway: token + reachability")
    p_msetup.add_argument("--gateway", default=None)
    p_msetup.add_argument("--token", default=None,
                          help="gateway token (else REACTORPRO_GATEWAY_TOKEN "
                               "or the gateway.env file)")
    p_msetup.add_argument("--token-path", default=None)
    p_msetup.add_argument("--timeout", type=float, default=5)
    p_gp = mesh_sub.add_parser(
        "gossip-push", help="Publish local gap fingerprints to peers")
    p_gp.add_argument("--gateway", default=None)
    p_gp.add_argument("--peers", required=True, help="comma-separated ids")
    p_gp.add_argument("--fingerprints", default=None)
    p_gp.add_argument("--audit", default=None)
    p_gp.add_argument("--origin", default="neuralosd")
    p_gp.add_argument("--timeout", type=float, default=30)
    p_gpl = mesh_sub.add_parser(
        "gossip-pull", help="Collect peer fingerprint blobs from the inbox")
    p_gpl.add_argument("--inbox", required=True)
    p_gpl.add_argument("--timeout", type=float, default=30)
    # default all mesh gateways to the real default
    for sub_p in (p_ms2, p_msend, p_mtc, p_mts, p_mtx, p_mtr,
                  p_mmb, p_mtr2, p_msetup):
        if not any(act.dest == "gateway" for act in sub_p._actions):
            sub_p.add_argument("--gateway", default=None)
    p_mr = mesh_sub.add_parser("register", help="Register this instance on the mesh")
    p_mr.add_argument("--instance-dir", required=True)
    p_mr.add_argument("--gateway", default="http://127.0.0.1:8090")
    p_mr.add_argument("--publish", action="store_true",
                      help="POST the registration blob to the gateway "
                           "(default: dry report)")
    p_mr.add_argument("--json", action="store_true")
    p_mp = mesh_sub.add_parser(
        "preflight", help="Would the gate answer this? (no probe runs)")
    p_mp.add_argument("--instance-dir", required=True)
    p_mp.add_argument("question", nargs="+")
    p_mdl = mesh_sub.add_parser(
        "delegate", help="Forward a question to the best peer (verified)")
    p_mdl.add_argument("--gateway", default=None)
    p_mdl.add_argument("question", nargs="+")
    p_mdl.add_argument("--instance-name", default="")
    p_mdl.add_argument("--visited", default="",
                       help="comma-separated agents already handling this "
                            "question (loop protection)")
    p_mdl.add_argument("--max-hops", type=int, default=3)
    p_mdl.add_argument("--deadline-ms", type=int, default=None)
    p_mdl.add_argument("--trust", default="known",
                       choices=["self", "trusted", "known", "untrusted"])
    p_mdl.add_argument("--preflight", action="store_true")
    p_mdl.add_argument("--timeout", type=float, default=30)
    p_mgap = mesh_sub.add_parser(
        "gaps", help="Cluster refused questions into trigger proposals")
    p_mgap.add_argument("--instance-dir", required=True)
    p_mgap.add_argument("--audit", default=None,
                        help="ask_audit.jsonl (default: <instance>/"
                             "ask_audit.jsonl)")
    p_mgap.add_argument("--min-count", type=int, default=2)
    p_mgap.add_argument("--limit", type=int, default=2000)
    p_mgap.add_argument("--json", action="store_true")
    p_mver = mesh_sub.add_parser(
        "verify", help="Verify a signed mesh envelope (stdin or --file)")
    p_mver.add_argument("--file", default=None)
    p_mver.add_argument("--key", default=None,
                        help="HMAC key (default $NEURALOSD_MESH_KEY)")
    p_ma = mesh_sub.add_parser("ask", help="Ask a question (through the local router)")
    p_ma.add_argument("--instance-dir", required=True)
    p_ma.add_argument("--gateway", default="http://127.0.0.1:8090")
    p_ma.add_argument("--delegate", action="store_true",
                      help="if the local gate refuses, discover peers and "
                           "delegate to the best one (the reply arrives "
                           "marked delegated + verified)")
    p_ma.add_argument("--preflight", action="store_true",
                      help="with --delegate: only consider peers whose own "
                           "gate would answer")
    p_ma.add_argument("--max-hops", type=int, default=3)
    p_ma.add_argument("--deadline-ms", type=int, default=None)
    p_ma.add_argument("--trust", default="known",
                      choices=["self", "trusted", "known", "untrusted"])
    p_ma.add_argument("question", nargs="+")
    p_mh = mesh_sub.add_parser("health", help="Check the Synapse gateway")
    p_mh.add_argument("--gateway", default="http://127.0.0.1:8090")

    # reason
    p_rs = sub.add_parser(
        "reason",
        help="Escalate a question to a reasoning model, then extend the menu")
    p_rs.add_argument("--instance-dir", required=True)
    p_rs.add_argument("question", nargs="*",
                      help="the question to escalate (not needed with --refresh)")
    p_rs.add_argument("--model", default=None,
                      help="Ollama model (default deepseek-r1:8b)")
    p_rs.add_argument("--ollama", default=None,
                      help="Ollama base URL (default http://127.0.0.1:11434)")
    p_rs.add_argument("--oracle", type=float, default=None,
                      help="an independently-computed expected RATIO; the "
                           "loop never sees where it came from")
    p_rs.add_argument("--scale", choices=["ratio", "percent"],
                      default="percent")
    p_rs.add_argument("--consensus-model", default=None,
                      help="a SECOND model that must independently agree "
                           "before the probe is installed")
    p_rs.add_argument("--shadow", action="store_true",
                      help="install the probe in shadow mode: evaluated, "
                           "not routable, until it agrees with its oracle")
    p_rs.add_argument("--shadow-days", type=int, default=14,
                      help="minimum days in shadow before promotion")
    p_rs.add_argument("--dry-run", action="store_true",
                      help="show the whole trail, write nothing")
    p_rs.add_argument("--refresh", action="store_true",
                      help="re-observe the instance and rewrite the derived "
                           "snapshot for the metrics already installed. No "
                           "model, no proposal. Use when the data moved.")
    p_rs.add_argument("--force", action="store_true",
                      help="escalate even when the gate says not to")
    p_rs.add_argument("--no-stability-check", action="store_true",
                      help="ask the model once instead of twice (faster, and "
                           "less safe)")
    p_rs.add_argument("--timeout", type=float, default=240.0)

    # door
    p_door = sub.add_parser(
        "door", help="Manage deployed instance doors (host -> sandbox)")
    door_sub = p_door.add_subparsers(dest="door_cmd")
    p_dl = door_sub.add_parser("list", help="List registered doors")
    p_dl.add_argument("--json", action="store_true")
    p_ds = door_sub.add_parser("stop", help="Stop a door's sandbox and forget it")
    p_ds.add_argument("name")
    p_dp = door_sub.add_parser(
        "proxy", help="Fallback relay for sandboxes created WITHOUT a native "
                      "forward (msb forwards are create-time only)")
    p_dp.add_argument("--name", required=True, help="sandbox name")
    p_dp.add_argument("--port", type=int, required=True, help="host port")
    p_dp.add_argument("--target", type=int, help="VM port (default: same)")
    p_dp.add_argument("--host", default="127.0.0.1")
    p_dp.add_argument("--backend", default="msb", choices=["msb", "boxlite"],
                      help="which exec channel the relay drives")

    # scrub
    p_scrub = sub.add_parser(
        "scrub", help="Sanitize cache + audit records already on disk")
    p_scrub.add_argument("--instance-dir", required=True)
    p_scrub.add_argument("--dry-run", action="store_true", help="report only")
    p_scrub.add_argument("--purge-cache", action="store_true",
                         help="delete the cache instead of rewriting it")

    # audit — instance import classification (frozen-binary gap)
    p_audit = sub.add_parser(
        "audit", help="Classify instance imports: bundled | host-only | missing-everywhere")
    p_audit.add_argument("--instance-dir", help="audit one instance")
    p_audit.add_argument("--root", help="audit every instance under DIR")
    p_audit.add_argument("--host-python",
                         help="sidecar/host python (default: $NEURALOSD_SIDECAR, "
                              "or this runtime when not frozen)")
    p_audit.add_argument("--json", action="store_true", help="machine-readable report")

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
        if getattr(a, "instances_dir", None):
            from ._cmd import serve as serve_mod
            serve_mod.run_multi(a)
        else:
            if not a.instance_dir:
                ap.error("serve requires --instance-dir or --instances-dir")
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
    elif a.command == "feedback":
        from ._cmd import feedback
        feedback.run(a)
    elif a.command == "mothball":
        from ._cmd import mothball
        mothball.run(a)
    elif a.command == "drift":
        from ._cmd import drift
        drift.run(a)
    elif a.command == "transfer":
        from ._cmd import transfer
        transfer.run(a)
    elif a.command == "shadow":
        from ._cmd import shadow
        shadow.run(a)
    elif a.command == "browser":
        from ._cmd import browser
        browser.run(a)
    elif a.command == "snapshot":
        from ._cmd import snapshot
        snapshot.run(a)
    elif a.command == "slo":
        from ._cmd import slo
        slo.run(a)
    elif a.command == "fleet":
        from ._cmd import fleet
        fleet.run(a)
    elif a.command == "badge":
        from ._cmd import badge
        badge.run(a)
    elif a.command == "synonyms":
        from ._cmd import synonyms
        synonyms.run(a)
    elif a.command == "temporal":
        from ._cmd import temporal
        raise SystemExit(temporal.run(a) or 0)
    elif a.command == "pi":
        from ._cmd import pi
        raise SystemExit(pi.run(a) or 0)
    elif a.command == "doctor":
        from ._cmd import doctor
        raise SystemExit(doctor.run(a) or 0)
    elif a.command == "roles":
        from ._cmd import roles
        raise SystemExit(roles.run(a) or 0)
    elif a.command == "watch":
        from ._cmd import watch
        raise SystemExit(watch.run(a) or 0)
    elif a.command == "actuate":
        from ._cmd import actuate
        raise SystemExit(actuate.run(a) or 0)
    elif a.command == "build":
        from ._cmd import build
        raise SystemExit(build.run(a) or 0)
    elif a.command == "openapi":
        from ._cmd import openapi
        openapi.run(a)
    elif a.command == "diff":
        from ._cmd import diff
        diff.run(a)
    elif a.command == "scrub":
        from ._cmd import scrub
        raise SystemExit(scrub.run(a))
    elif a.command == "audit":
        from ._cmd import audit
        audit.run(a)
    elif a.command == "mesh":
        if getattr(a, "mesh_cmd", None) == "gossip-push":
            from .loop import cluster_gaps, export_fingerprints, gossip_push
            blob = _gossip_blob(a)
            transport = _gossip_transport(a)
            peers = [p.strip() for p in (a.peers or "").split(",")
                     if p.strip()]
            report = gossip_push(transport, blob, peers, origin=a.origin)
            print(json.dumps(report, indent=2))
            raise SystemExit(0)
        if getattr(a, "mesh_cmd", None) == "gossip-pull":
            from .loop import gossip_collect
            state = gossip_collect(a.inbox)
            print(json.dumps(state, indent=2, ensure_ascii=False))
            raise SystemExit(0)
        from ._cmd import mesh
        raise SystemExit(mesh.run(a))
    elif a.command == "door":
        from ._cmd import door
        raise SystemExit(door.run(a))
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
