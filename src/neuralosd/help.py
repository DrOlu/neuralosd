"""Comprehensive help catalogue for the neuralosd CLI.

Every subcommand gets a full description, its usage line, runnable examples,
exit-code semantics, whether it mutates state, and what to read next. The
same catalogue feeds ``-h/--help`` on every parser and
``neuralosd help --json``, so an orchestrating agent can discover and drive
the whole surface without guessing.

Design rule: help text is part of the product contract. It is written for a
reader who has never seen the repository, with concrete commands that run as
printed.
"""
import argparse
import json
import sys

from . import __version__

CONTRACT = """\
neuralOSd answers questions through a CLOSED probe menu. A question either
maps to exactly one probe, or it is refused — the runtime never guesses.

Refusals carry a JSON envelope with `refusal_reason`:
  no_probe_matches        nothing in the menu fits
  below_floor             scores are too weak or tied
  dropped_filter          the question names a filter the probe cannot apply
  action_intent           the question asks for an action, not an answer
  args_not_extractable    the probe needs an argument the question did not give
  low_coverage            the question is not covered by the probe

Exit codes are stable: 0 = answered / check passed, 2 = refused (for `ask`),
1 or other = operational failure. Many commands offer `--json` for machines.
There is no shell escape and no free-form SQL: probes are the only capability.
"""


def _t(summary, description, usage, examples, *, exits=None, safety=None,
       see=(), json_=None, mutates=False):
    return {
        "summary": summary,
        "description": description.strip(),
        "usage": usage,
        "examples": list(examples),
        "exits": exits,
        "safety": safety,
        "see": list(see),
        "json": json_,
        "mutates": mutates,
    }


TOPICS = {
    "init": _t(
        "Scaffold a new instance (probe menu) from a data source.",
        """Creates an instance directory from a CSV, JSON, SQLite, XLSX, syslog
or database URL: it profiles the source, writes a starter `probes.py` and
`needle_menu.json`, and records the menu version. Run `ask` against the new
directory immediately — scaffolded probes answer without an edit first.
Use `--extra-source` to join a second source (the probes get the extra
connection), and `--packages` when a source needs a host library.""",
        "neuralosd init --source SRC --name NAME [--out DIR] [options]",
        ["neuralosd init --source ./sales_2024.csv --name global-sales",
         "neuralosd init --source postgresql://user:pass@host/analytics "
         "--name fin --packages pandas",
         "neuralosd init --source syslog:/var/log/system.log --name syslog "
         "--header 'X-Tenant: acme' --secret-ref pg_password"],
        exits="0 created; 1 source could not be read or profiled",
        see=["ask", "lint", "golden"], mutates=True),

    "deploy": _t(
        "Deploy an instance into a hardware-isolated sandbox.",
        """Builds and starts the instance inside BoxLite or Microsandbox, with
its data mounted read-only and the runtime sealed in a microVM. The door
records the host port so `serve`/HTTP clients reach it without opening the
guest. Templates clone a known-good box instead of rebuilding.""",
        "neuralosd deploy --instance-dir DIR --name NAME "
        "[--backend boxlite|msb] [options]",
        ["neuralosd deploy --instance-dir ./global-sales --backend boxlite "
         "--name sales-prod --port 8501",
         "neuralosd deploy --instance-dir ./global-sales --name sales-2 "
         "--backend msb --from-template sales-gold",
         "neuralosd deploy --instance-dir ./global-sales --name sales-gold "
         "--save-template sales-gold --force"],
        exits="0 deployed; 1 backend missing or the box failed to start",
        safety="Starts a long-running sandbox. `door stop NAME` shuts it down.",
        see=["door", "backends", "sidecar"], mutates=True),

    "plasma": _t(
        "Drive the Plasma EDR through its published command catalog.",
        """Plasma publishes every command as `plasma help --json` (schema
plasma.help/1) with a safety class. `plasma catalog` lists them; `plasma
init` turns every generatable READ-ONLY command into a neuralosd probe, so
the whole read surface is answerable in plain English. Plan-gated commands
are two-step: `plasma plan` records Plasma's dry-run, `plasma apply`
executes exactly that plan. There is no arbitrary execution and no shell.
Subcommands: catalog, init, probes, call, plan, apply, pending, health.""",
        "neuralosd plasma {catalog|init|probes|call|plan|apply|pending|health} "
        "[options]",
        ["neuralosd plasma catalog --read-only",
         "neuralosd plasma init --out ./plasma-estate --root .plasma",
         "neuralosd plasma call health",
         "neuralosd plasma plan 'enforce run'",
         "neuralosd plasma apply plp_0123456789ab"],
        exits="0 ok; 1 Plasma refused/failed; 2 invalid command",
        safety="`apply` executes a previously recorded plan-gated command.",
        see=["init", "ask", "actuate"]),

    "mesh": _t(
        "Talk to other neuralOSd peers over the Synapse A2A mesh.",
        """Serve an instance's capabilities to peers, discover who can answer a
question, delegate asks, exchange tasks and mail, and gossip gap
fingerprints (hashes, never raw questions). Trust is pinned by peer
fingerprint; hop and loop limits apply to delegation. Subcommands:
serve, register, discover, agents, ask, delegate, send, mailbox, task-create,
task-status, task-cancel, task-retry, trust, setup, gossip-push,
gossip-pull, preflight, gaps, verify, health.""",
        "neuralosd mesh SUBCOMMAND [options]",
        ["neuralosd mesh serve --instance-dir ./global-sales --publish",
         "neuralosd mesh send --target finance-edge 'total revenue by region'",
         "neuralosd mesh delegate 'q3 margin split' --max-hops 2",
         "neuralosd mesh health"],
        exits="depends on the subcommand; 0 ok, 1 gateway/peer error",
        safety="Serving opens a listener; delegate/send reach other machines.",
        see=["ask", "serve"]),

    "door": _t(
        "Manage deployed instance doors (host → sandbox).",
        """A door is the recorded transport into a deployed instance. `list`
shows name, backend, ports and status; `stop` shuts the sandbox down;
`proxy` adds a host relay for backends whose port forward is create-time
only (Microsandbox).""",
        "neuralosd door {list|stop|proxy} [options]",
        ["neuralosd door list --json",
         "neuralosd door stop sales-prod",
         "neuralosd door proxy --name sales-prod --port 8501 --target 8420 "
         "--host 0.0.0.0"],
        exits="0 ok; 1 unknown door or backend error",
        safety="`stop` terminates the sandbox; `proxy` opens a host port.",
        see=["deploy"], mutates=True),

    "backends": _t(
        "List the sandbox backends available on this host.",
        """Reports whether BoxLite and Microsandbox are installed and importable,
with the pip extra that installs each. Both use the same probe contract, so
switching backends never changes an instance.""",
        "neuralosd backends",
        ["neuralosd backends"],
        exits="0 always",
        see=["deploy"]),

    "sidecar": _t(
        "Provision a sidecar Python environment for host-only libraries.",
        """A sealed single-file binary cannot import host-installed libraries.
The sidecar creates a separate Python environment the binary can call for
data access (pypdf, pywinrm, a database driver), keeping routing,
verification and serving inside the binary. `--status` reports it without
changing anything.""",
        "neuralosd sidecar [--status|--setup] [--with PKGS] [--python 3.12]",
        ["neuralosd sidecar --status",
         "neuralosd sidecar --python /usr/bin/python3 --force",
         "neuralosd sidecar --setup --with pypdf,pywinrm"],
        exits="0 ready; 1 setup failed",
        safety="`--setup` creates/modifies a Python environment on disk.",
        see=["audit", "doctor"], mutates=True),

    "ask": _t(
        "Ask a natural-language question; get a verified answer or a refusal.",
        """Routes the question through the closed menu, runs exactly one probe,
applies the results gate and prints the answer envelope. A refusal is a
first-class result: inspect `refusal_reason` and `scores` rather than trying
another phrasing blindly. `--role` applies an ACL role's probe allowlist and
row filter; `--as-of` answers from a pinned snapshot; `--strict` refuses on
any discarded filter instead of answering partially.""",
        "neuralosd ask --instance-dir DIR [--role R] [--as-of DATE] "
        "[--strict] QUESTION",
        ["neuralosd ask --instance-dir ./global-sales "
         "'total revenue by region'",
         "neuralosd ask --instance-dir ./syslog --role oncall --as-of "
         "2026-12-01 'failed logins on ws1'",
         "neuralosd ask --strict --instance-dir ./fin "
         "'net margin per quarter'"],
        exits="0 answered; 2 refused (JSON envelope with refusal_reason); "
              "1 operational failure",
        see=["gaps", "serve", "golden", "roles"]),

    "serve": _t(
        "Start the authenticated HTTP API (OpenAPI 3.1 at /openapi.json).",
        """Serves every instance over `/v1`: discovery, ask, typed probe runs,
plans, audit, maintenance operations and (opt-in) mesh delegation. The
OpenAPI document is generated from the same route table that dispatches, so
it cannot drift. Auth is Bearer or `X-API-Key`; keys are compared in
constant time and never logged. Loopback is the default; a wide bind with
the default lab key is refused. `--read-only` refuses every mutating route;
`--allow-mesh` enables the three mesh routes.""",
        "neuralosd serve --instance-dir DIR [--instance-dir DIR ...] "
        "--port 9998 --api-key KEY [options]",
        ["neuralosd serve --instance-dir ./global-sales --port 9998 "
         "--api-key \"$KEY\"",
         "neuralosd serve --instance-dir ./a --instance-dir ./b --port 9998 "
         "--api-key \"$KEY\" --read-only --tls-cert cert.pem --tls-key key.pem",
         "neuralosd serve --instance-dir ./sales --read-only --allow-mesh"],
        exits="0 stopped normally; 1 bind/auth/TLS failure",
        safety="`--read-only` recommended for query-only deployments. Never "
               "expose the default lab key beyond loopback.",
        see=["ask", "version"], mutates=False),

    "reason": _t(
        "Escalate one question to a reasoning model, then extend the menu.",
        """When the gate refuses and a human wants the gap closed, `reason` asks
an Ollama reasoning model for the missing capability, checks its proposal
against a stability/consensus test, then writes a draft probe. `--shadow`
keeps the draft out of the serving menu until `shadow --promote` sees it
agree with oracles.""",
        "neuralosd reason --instance-dir DIR [--model M] [--shadow] QUESTION",
        ["neuralosd reason --instance-dir ./global-sales "
         "'why did EMEA margins dip in Q3' --consensus-model qwen3:8b",
         "neuralosd reason --shadow --instance-dir ./global-sales "
         "'average order value by month'"],
        exits="0 draft written; 1 model unreachable or proposal rejected",
        safety="Writes probes and can extend the menu. `--shadow` avoids "
               "serving them immediately.",
        see=["build", "shadow", "gaps"], mutates=True),

    "browser": _t(
        "Drive a named agent-browser session (open, snapshot, click, fill).",
        """Talks to a running agent-browser control socket so a probe (or an
operator) can inspect and act on a web page. Sessions keep cookies and
tabs; refs come from `snapshot`.""",
        "neuralosd browser {open|snapshot|click|fill|type|network|text|"
        "setup} [--session S] [--url URL] [--target REF] [--text TEXT]",
        ["neuralosd browser open --url https://status.internal.example "
         "--session ops",
         "neuralosd browser snapshot --session ops",
         "neuralosd browser click --target e12 --session ops",
         "neuralosd browser fill --target e7 --text 'acme' --session ops"],
        exits="0 action sent; 1 browser/session unavailable",
        safety="`click`/`fill` act on a live web page.",
        see=["doctor"], mutates=True),

    "build": _t(
        "Natural language in, a VERIFIED capability out.",
        """The authoring loop: inspect the data, propose a probe, write it, run
it, and only keep it if it answers correctly. `--durable` runs the build
under pi-durable so it survives a crash; `--resume` continues an interrupted
build. `--provider` selects the coding agent backend.""",
        "neuralosd build --instance-dir DIR [--durable] [--resume] [options] "
        "REQUEST",
        ["neuralosd build --instance-dir ./global-sales "
         "'shipping cost per unit by region'",
         "neuralosd build --instance-dir ./global-sales --durable "
         "'return rate by product line'",
         "neuralosd build --instance-dir ./global-sales --provider superagent "
         "--timeout 1800 'cohort retention by month'"],
        exits="0 verified probe added; 1 proposal failed verification",
        safety="Adds probes to the instance (menu version changes).",
        see=["pi", "golden", "gaps"], mutates=True),

    "pi": _t(
        "Drive the pi coding agent (operator mode and durable setup).",
        """Runs a pi session against the instance for operator work (inspect the
banks, patch a probe, run tests). Durable mode manages a long-running build:
`--status` reports it, `--steer` sends guidance, `--stop` ends it.""",
        "neuralosd pi --instance-dir DIR [--durable --status|--steer|--stop] "
        "[PROMPT]",
        ["neuralosd pi --instance-dir ./global-sales 'inspect the banks layout'",
         "neuralosd pi --instance-dir ./global-sales --durable --status",
         "neuralosd pi --instance-dir ./global-sales --steer 'use polars'",
         "neuralosd pi --instance-dir ./global-sales --stop"],
        exits="0 ok; 1 pi/node/durable package unavailable",
        safety="Operator mode can edit the instance; review changes.",
        see=["build", "doctor"], mutates=True),

    "temporal": _t(
        "Durable execution via Temporal (setup, worker, activities, schedule).",
        """Checks or uses the Temporal CLI, server and Python SDK. `--worker`
runs a durable worker for an instance; `--run-activity` executes one named
activity; `--schedule-create` registers a cron schedule.""",
        "neuralosd temporal {--setup|--worker|--run-activity|"
        "--schedule-create} [options]",
        ["neuralosd temporal --setup",
         "neuralosd temporal --worker --instance-dir ./global-sales",
         "neuralosd temporal --run-activity loop_activity "
         "--instance-dir ./global-sales",
         "neuralosd temporal --schedule-create --instance-dir ./global-sales "
         "--cron '0 * * * *'"],
        exits="0 ok; 1 Temporal components missing or the run failed",
        see=["doctor", "build"], mutates=True),

    "gaps": _t(
        "Mine gated/fuzzy questions from the ask ledger into proposals.",
        """Clusters refused and partially answered questions by intent so each
cluster becomes one authoring task, with the cost framing attached.
`--export-fingerprints` emits portable gap fingerprints for mesh gossip;
`--ingest-fingerprints` merges peers' without leaking their questions.""",
        "neuralosd gaps AUDIT.jsonl [--menu MENU] [options]",
        ["neuralosd gaps ./global-sales/ask_audit.jsonl "
         "--menu ./global-sales/needle_menu.json",
         "neuralosd gaps ./global-sales/ask_audit.jsonl "
         "--export-fingerprints ./fps.json",
         "neuralosd gaps ./global-sales/ask_audit.jsonl "
         "--ingest-fingerprints ./peers.json"],
        exits="0 report printed; 1 unreadable ledger",
        see=["synonyms", "build", "slo"]),

    "synonyms": _t(
        "Propose (and apply) trigger synonyms from refusal history.",
        """Reads the ask ledger, finds phrasings that were refused although a
probe could answer them, and proposes trigger additions. Review the
proposal file; `--apply` updates the menu with a new menu version.""",
        "neuralosd synonyms --instance-dir DIR [--apply proposals.json]",
        ["neuralosd synonyms --instance-dir ./global-sales",
         "neuralosd synonyms --instance-dir ./global-sales "
         "--apply proposals.json"],
        exits="0 proposals/report; 1 apply failed validation",
        safety="`--apply` mutates the menu.",
        see=["gaps", "lint"], mutates=True),

    "transfer": _t(
        "Propose a schema-sibling's probes for a new source.",
        """Compares two sources' profiles, matches tables/columns by structural
similarity, and emits a transfer proposal at the requested minimum
similarity. The proposal still has to pass `golden`/`truth` before earning
its way into service.""",
        "neuralosd transfer --from SRC --to SRC [--min-sim 0.6] [--out FILE]",
        ["neuralosd transfer --from ./sales_2024.csv --to ./sales_2025.csv "
         "--min-sim 0.6 --out transfer.json"],
        exits="0 proposal written; 1 source could not be profiled",
        see=["diff", "init"], mutates=False),

    "shadow": _t(
        "Canary new probes without serving them.",
        """Shadow probes run alongside the menu, are evaluated against truth
oracles or repeated observations, and only `--promote` when agreement and
run count clear the thresholds. This is how new capabilities earn trust
without risking answers.""",
        "neuralosd shadow --instance-dir DIR [--evaluate] [--promote]",
        ["neuralosd shadow --instance-dir ./global-sales",
         "neuralosd shadow --instance-dir ./global-sales --evaluate",
         "neuralosd shadow --instance-dir ./global-sales --promote "
         "--min-agreement 0.98 --min-runs 20"],
        exits="0 report/ok; 1 promotion refused (agreement too low)",
        safety="`--promote` changes the serving menu.",
        see=["reason", "build"], mutates=True),

    "feedback": _t(
        "Turn a rejected answer into a golden/trap candidate.",
        """Captures the operator's judgment on one ask. `--verdict bad` means
the question was legitimate but the answer was wrong: it records a golden
CANDIDATE with the optional `--correction` attached. `--verdict invalid`
means the question is out of scope: it records a trap candidate, promoted
only when the refusal is proven. `--apply` reviews and promotes the
accumulated candidates into the banks.""",
        "neuralosd feedback --instance-dir DIR (--question Q... --verdict "
        "bad|invalid [--correction TEXT] | --apply)",
        ["neuralosd feedback --instance-dir ./global-sales "
         "--question 'net margin' --verdict invalid",
         "neuralosd feedback --instance-dir ./global-sales "
         "--question 'units by region' --verdict bad "
         "--correction 'revenue by region' --by ada",
         "neuralosd feedback --instance-dir ./global-sales --apply"],
        exits="0 candidate recorded/promoted; 1 invalid reference",
        safety="Writes bank candidates; `--apply` promotes them.",
        see=["golden", "calibrate"], mutates=True),

    "mothball": _t(
        "Demote probes nobody queries (kept, reinstatable).",
        """Finds probes with no successful asks in the age window and demotes
them out of the serving menu, recording why. Demoted probes stay in the
menu file and return with `--reinstate`; nothing is deleted.""",
        "neuralosd mothball --instance-dir DIR [--age-days N] [--apply] "
        "[--reinstate ID]",
        ["neuralosd mothball --instance-dir ./global-sales --age-days 90",
         "neuralosd mothball --instance-dir ./global-sales --age-days 90 "
         "--apply",
         "neuralosd mothball --instance-dir ./global-sales --reinstate probe_id"],
        exits="0 report/ok; 1 unknown probe",
        safety="`--apply` changes the serving menu; `--reinstate` restores one.",
        see=["lint", "golden"], mutates=True),

    "drift": _t(
        "Re-audit an instance against a changed source schema.",
        """Profiles the new source, diffs it against the menu's recorded
schema, and reports probes whose columns/tables changed underneath them.
Broken probes are named before they answer with stale assumptions.""",
        "neuralosd drift --instance-dir DIR --source NEW_SOURCE",
        ["neuralosd drift --instance-dir ./global-sales "
         "--source ./sales_2025.xlsx"],
        exits="0 report; 1 source unreadable",
        see=["diff", "init"], mutates=False),

    "snapshot": _t(
        "Pin a hashed snapshot of every probe observation.",
        """Runs the instance's probes and records their results (and hashes)
under a label, so a later `ask --as-of` can answer from that frozen state
and `diff` can show what changed.""",
        "neuralosd snapshot --instance-dir DIR --label LABEL",
        ["neuralosd snapshot --instance-dir ./global-sales "
         "--label pre-migration-2026-12"],
        exits="0 pinned; 1 probe run failed",
        safety="Writes a snapshot under the instance directory.",
        see=["ask", "diff"], mutates=True),

    "golden": _t(
        "Run (or extend) the golden question bank.",
        """The routing regression test: every golden case must map to its
expected probe, or refuse when expected. Exit 1 on any mismatch makes it a
CI gate. Adding a case requires `--record` with `--question ...
--expect-probe ...`; without `--record` those flags are ignored and the
existing bank is re-run.""",
        "neuralosd golden --dir DIR [--record --question Q --expect-probe P | "
        "--expect-refusal] [options]",
        ["neuralosd golden --dir ./global-sales",
         "neuralosd golden --dir ./global-sales --record "
         "--question 'total revenue' --expect-probe revenue_by_region",
         "neuralosd golden --dir ./global-sales --record "
         "--question 'forecast 2030' --expect-refusal --value null"],
        exits="0 all cases pass; 1 any case fails",
        safety="Adding a case mutates golden.json.",
        see=["truth", "invariants", "calibrate"], mutates=True),

    "truth": _t(
        "Run the truth oracle (database instances).",
        """Computes expected values independently of the probes and compares
them with what the probes return — the strongest verification the runtime
offers. `--audit FILE` sends the self-test audit rows to FILE instead of
the default self-test ledger.""",
        "neuralosd truth --dir DIR [--audit FILE]",
        ["neuralosd truth --dir ./global-sales",
         "neuralosd truth --dir ./global-sales "
         "--audit ./global-sales/ask_audit.selftest.jsonl"],
        exits="0 all oracles agree; 1 any mismatch",
        see=["golden", "invariants"]),

    "invariants": _t(
        "Run the instance's property checks.",
        """Executes `invariants.py` (row-count sums, monotonicity, referential
integrity — whatever the instance declares) and fails if a property is
violated. Cheap, deterministic, and safe to run in CI.""",
        "neuralosd invariants --dir DIR",
        ["neuralosd invariants --dir ./global-sales"],
        exits="0 all hold; 1 any violation",
        see=["truth", "golden"]),

    "calibrate": _t(
        "Learn per-probe confidence gates from the golden and trap banks.",
        """Fits the refusal floor/margin from what golden cases must answer and
traps must refuse, writes `router.json`, and reloads it. `--traps` names the
trap bank; `--allow-no-traps` permits a one-sided fit (less safe).""",
        "neuralosd calibrate [--golden GOLDEN.json] [--traps TRAPS.json] "
        "[--allow-no-traps]",
        ["neuralosd calibrate --golden ./global-sales/golden.json "
         "--traps ./global-sales/traps.json",
         "neuralosd calibrate --golden ./global-sales/golden.json "
         "--allow-no-traps"],
        exits="0 fitted; 1 banks inconsistent or no traps without the flag",
        safety="Rewrites router.json (changes the refusal boundary).",
        see=["golden", "slo"], mutates=True),

    "lint": _t(
        "Trigger-collision linter for a probe menu.",
        """Finds probes whose triggers overlap so closely that routing is
ambiguous, ranks the collisions, and reports the pairs a synonym or trigger
edit should separate. `--top` limits the list.""",
        "neuralosd lint MENU.json [--top N]",
        ["neuralosd lint ./global-sales/needle_menu.json --top 10"],
        exits="0 report (also when collisions exist); 1 unreadable menu",
        see=["synonyms", "golden"]),

    "openapi": _t(
        "Generate OpenAPI 3.1 + an MCP manifest from a probe menu.",
        """Emits the probe-level contract (typed request bodies, responses) for
client generation and writes an MCP manifest. `--agent NAME` is the agent
name recorded inside that manifest; the file itself is always written as
`mcp_manifest.json` beside the menu. The live server's own document is at
`/openapi.json`; this command works offline from the menu file.""",
        "neuralosd openapi MENU.json [--agent NAME]",
        ["neuralosd openapi ./global-sales/needle_menu.json",
         "neuralosd openapi ./global-sales/needle_menu.json "
         "--agent global-sales"],
        exits="0 written; 1 invalid menu",
        see=["serve"], mutates=False),

    "diff": _t(
        "Profile and diff two sources (schema evolution).",
        """Compares old and new profiles: tables/columns added, removed or
retyped, with the probes affected by each change. The report is what a
migration review needs before `drift` and `golden` re-run.""",
        "neuralosd diff OLD NEW [--menu MENU]",
        ["neuralosd diff ./old.csv ./new.csv "
         "--menu ./global-sales/needle_menu.json"],
        exits="0 report; 1 source unreadable",
        see=["drift", "transfer"]),

    "slo": _t(
        "Refusal-rate SLO for one instance.",
        """Quantifies the menu-gap signal over a window: asks, refusals, refusal
rate, and whether it breaches the configured maximum. Exits 1 on a breach
so a scheduler or CI job fails loudly; with too few asks it says so instead
of pretending the rate is meaningful.""",
        "neuralosd slo --instance-dir DIR [--window-hours N] "
        "[--max-refusal-rate R] [--min-asks N]",
        ["neuralosd slo --instance-dir ./global-sales --window-hours 168 "
         "--max-refusal-rate 0.15"],
        exits="0 within SLO (or insufficient data); 1 breach",
        see=["gaps", "fleet"]),

    "fleet": _t(
        "Multi-instance health dashboard.",
        """Runs the same health/refusal check across a list of instance
directories and prints one table, failing if any instance is unhealthy.
Use it as the host-level uptime check.""",
        "neuralosd fleet DIR [DIR ...] [--max-refusal-rate R]",
        ["neuralosd fleet ~/instances/finance ~/instances/sales "
         "~/instances/syslog --max-refusal-rate 0.2"],
        exits="0 all healthy; 1 any instance unhealthy",
        see=["slo", "doctor"]),

    "badge": _t(
        "Golden-bank health badge and a GitHub Actions snippet.",
        """Runs the golden bank and emits a shields-style JSON badge (optionally
a workflow file) that reports the bank as passing or failing — the README
badge that cannot lie because it is generated from a real run.""",
        "neuralosd badge --instance-dir DIR [--out FILE] "
        "[--actions-out FILE]",
        ["neuralosd badge --instance-dir ./global-sales",
         "neuralosd badge --instance-dir ./global-sales "
         "--actions-out .github/workflows/golden-badge.yml"],
        exits="0 badge written; 1 bank failed or output unwritable",
        safety="`--actions-out` writes a workflow file.",
        see=["golden"], mutates=True),

    "doctor": _t(
        "Health-check every integration this host needs.",
        """Checks the engines (pi, pi-durable), agent-browser, Temporal CLI and
SDK, gateway credentials, data libraries, the sandbox backends, and the
instance banks when `--instance-dir` is given. JSON or human output; exits
non-zero when anything fails.""",
        "neuralosd doctor [--instance-dir DIR] [--json]",
        ["neuralosd doctor",
         "neuralosd doctor --instance-dir ./global-sales --json"],
        exits="0 all checks pass; 1 any check fails",
        see=["audit", "backends"]),

    "audit": _t(
        "Classify an instance's imports: bundled, host-only, missing.",
        """Walks every probe's imports and says where each library comes from:
bundled in the binary, available only on the host (sidecar or full Python),
or missing everywhere (the probe will fail at ask time). Run it in CI
before shipping an instance.""",
        "neuralosd audit --instance-dir DIR | --root DIR [--json]",
        ["neuralosd audit --instance-dir ./global-sales",
         "neuralosd audit --root ~/instances --json"],
        exits="0 report; 1 an import is missing everywhere",
        see=["sidecar", "doctor"]),

    "actuate": _t(
        "Named, journaled, reversible action (never free-form shell).",
        """Records a plan for a named action against a target, requires a
reviewer and reason, and only `--apply` performs it. `--undo --undo-plan`
reverses an applied plan from the journal. The action catalogue is fixed;
nothing here executes arbitrary commands.""",
        "neuralosd actuate --instance-dir DIR --action NAME --target T "
        "--operator WHO --reason R [--apply --plan P] "
        "[--undo --undo-plan P]",
        ["neuralosd actuate --instance-dir ./security --action block_ip "
         "--target 10.0.0.42 --operator ada --reason 'incident 4711'",
         "neuralosd actuate --instance-dir ./security --action block_ip "
         "--target 10.0.0.42 --operator ada --reason 'incident 4711' "
         "--apply --plan PLAN-8f3",
         "neuralosd actuate --instance-dir ./security --undo "
         "--undo-plan PLAN-8f3"],
        exits="0 planned/applied/undone; 1 refused or plan invalid",
        safety="`--apply` performs the side effect; undo needs the plan id.",
        see=["serve"], mutates=True),

    "watch": _t(
        "Stream sources into a bounded buffer and alert on rules.",
        """Tails a source (syslog, HTTP, journald, wevtutil), keeps a bounded
in-memory window, evaluates declarative rules, and can post matches to a
webhook. `--once` drains the current data and exits, which is what cron
wants.""",
        "neuralosd watch --instance-dir DIR --source SRC "
        "[--rules R.json] [--webhook URL] [--once]",
        ["neuralosd watch --instance-dir ./syslog "
         "--source syslog:/var/log/auth.log",
         "neuralosd watch --instance-dir ./syslog --rules rules.json "
         "--webhook https://ops.example/hook --once"],
        exits="0 normal stop; 1 source/rule error",
        safety="Runs until stopped (unless `--once`); posts to the webhook.",
        see=["ask", "temporal"], mutates=False),

    "roles": _t(
        "Manage acl.json roles (probe allowlist + row filter).",
        """Adds, lists, shows or removes roles that constrain what an
`ask --role` caller may run: a comma-separated probe allowlist and an
optional row filter written `column=in:value1,value2`. Roles are enforced
before execution, not after.""",
        "neuralosd roles --instance-dir DIR --op list|show|set|remove "
        "[--role R] [--allow A,B] [--row-filter 'COL=in:V1,V2']",
        ["neuralosd roles --instance-dir ./fin --op list",
         "neuralosd roles --instance-dir ./fin --op set --role analyst "
         "--allow revenue_by_region,margin_by_quarter",
         "neuralosd roles --instance-dir ./fin --op set --role analyst "
         "--row-filter 'region=in:EMEA'",
         "neuralosd roles --instance-dir ./fin --op remove --role intern"],
        exits="0 ok; 1 invalid role/filter",
        safety="`set`/`remove` rewrite acl.json.",
        see=["ask"], mutates=True),

    "scrub": _t(
        "Sanitize cache and audit records already on disk.",
        """Rewrites (or with `--purge-cache`, deletes) cached answers and audit
rows that may contain PII, preserving the audit chain where possible. Use
it after a data-export incident, not as routine maintenance.""",
        "neuralosd scrub --instance-dir DIR [--dry-run] [--purge-cache]",
        ["neuralosd scrub --instance-dir ./syslog",
         "neuralosd scrub --instance-dir ./syslog --purge-cache"],
        exits="0 rewritten; 1 unreadable ledger",
        safety="Irreversibly rewrites/deletes on-disk records.",
        see=["ask"], mutates=True),

    "docs": _t(
        "Read the bundled documentation (works offline).",
        """Prints one of the shipped long-form documents (usage, architecture,
cookbook, reference) from inside the install — no internet, no repository,
which is what an air-gapped operator needs.""",
        "neuralosd docs [TOPIC]",
        ["neuralosd docs", "neuralosd docs usage",
         "neuralosd docs architecture"],
        exits="0 printed; 1 unknown topic",
        see=["help"]),

    "version": _t(
        "Print the installed version and install shape.",
        """Prints the semantic version. `--json` adds how this copy was installed
(`pip` or `frozen`), which is what `update` uses to choose a strategy.""",
        "neuralosd version [--json]",
        ["neuralosd version", "neuralosd version --json",
         "neuralosd --version"],
        exits="0 always",
        see=["update"]),

    "update": _t(
        "Check for, or apply, a newer release (verified).",
        """`--check` reports current and latest and changes nothing; without it
the command asks (or takes `--yes`)
and applies. pip/uv/pipx installs upgrade through the running
interpreter's package manager; standalone binaries download the platform
asset, verify the release's sha256 digest, keep a `.bak` and swap
atomically. Editable checkouts are refused. No silent background updater:
nothing runs unless this command does.""",
        "neuralosd update [--check] [--yes] [--force] [--channel stable|pre] "
        "[--json]",
        ["neuralosd update --check",
         "neuralosd update --check --json",
         "neuralosd update --yes",
         "neuralosd update --channel pre"],
        exits="0 up to date / updated / declined; 1 network, digest or "
              "package-manager failure",
        safety="Applies an update when confirmed; keeps the previous binary "
               "or package manager transaction.",
        see=["version"]),

    "help": _t(
        "Show this catalogue, one command's help, or machine-readable JSON.",
        """`neuralosd help` lists every command with a one-line purpose.
`neuralosd help ask` prints the same detailed page as `ask --help`.
`neuralosd help --json` emits the whole catalogue — descriptions, usage,
examples, exit codes, flags and defaults — so an orchestrating agent can
discover the CLI without parsing prose.""",
        "neuralosd help [COMMAND] [--json]",
        ["neuralosd help",
         "neuralosd help ask",
         "neuralosd help --json"],
        exits="0 printed; 2 unknown command",
        see=["docs", "version"]),
}


MESH = {
    "serve": _t(
        "Serve this instance on the mesh (Synapse).",
        "Registers this instance and listens for mesh asks, heartbeating its "
        "skills so peers can discover it. `--publish` advertises the "
        "instance's probes as skills immediately.",
        "neuralosd mesh serve --instance-dir DIR [--publish] "
        "[--heartbeat SECONDS]",
        ["neuralosd mesh serve --instance-dir ./global-sales --publish "
         "--heartbeat 30"],
        exits="0 stopped; 1 gateway unreachable",
        safety="Opens a network listener through the gateway.",
        see=["mesh.register", "mesh.discover"], mutates=False),
    "discover": _t(
        "Discover peers that could answer a question or skill.",
        "Asks the gateway for peers whose advertised skills/capabilities "
        "match the text. `--preflight` also asks each peer whether it would "
        "answer (no probe runs).",
        "neuralosd mesh discover [--skill TEXT] [--instance-name NAME] "
        "[--preflight] [--json]",
        ["neuralosd mesh discover --skill 'revenue' --preflight",
         "neuralosd mesh discover --skill 'revenue' --json"],
        exits="0 list printed; 1 gateway unreachable",
        see=["mesh.delegate", "mesh.ask"]),
    "agents": _t(
        "Print the peer directory (phonebook).",
        "Lists known peers, their addresses, skills, trust state and last "
        "heartbeat. `--capabilities` filters by advertised capability.",
        "neuralosd mesh agents [--capabilities TEXT]",
        ["neuralosd mesh agents"],
        exits="0 printed; 1 gateway unreachable",
        see=["mesh.discover"]),
    "ask": _t(
        "Ask through the local router, optionally delegating to the mesh.",
        "Runs the question locally first; `--delegate` lets the mesh answer "
        "when the local menu refuses, and `--preflight` only reports who "
        "would answer without running a probe.",
        "neuralosd mesh ask --instance-dir DIR [--delegate] [--preflight] "
        "[--max-hops N] [--deadline-ms MS] QUESTION",
        ["neuralosd mesh ask --instance-dir ./global-sales --delegate "
         "--preflight 'q3 margin split'"],
        exits="0 answered; 2 refused; 1 mesh/gateway error",
        see=["ask", "mesh.delegate"]),
    "delegate": _t(
        "Delegate a question to a mesh peer.",
        "Chooses a peer and forwards the question within hop, visited-set and "
        "deadline limits, returning the peer's envelope.",
        "neuralosd mesh delegate QUESTION... [--max-hops N] "
        "[--deadline-ms MS] [--trust known|any]",
        ["neuralosd mesh delegate 'q3 margin split by product line' "
         "--max-hops 2 --deadline-ms 60000"],
        exits="0 answered; 2 refused by the peer; 1 no route/timeout",
        see=["mesh.discover", "mesh.ask"]),
    "send": _t(
        "Send a message to a peer.",
        "Posts a text message to a named peer's mailbox over the gateway.",
        "neuralosd mesh send --target PEER TEXT... [--timeout-ms N]",
        ["neuralosd mesh send --target finance-edge "
         "'total revenue by region'"],
        exits="0 delivered; 1 gateway/peer error",
        see=["mesh.mailbox"]),
    "task-create": _t(
        "Create a task on a peer.",
        "Creates a named task for a peer carrying a question/payload; the "
        "gateway mints a task id (or use --task-id for an idempotent retry).",
        "neuralosd mesh task-create --target PEER QUESTION... [--task-id ID]",
        ["neuralosd mesh task-create --target reporting-edge "
         "'build the nightly report'"],
        exits="0 task created; 1 gateway error",
        see=["mesh.task-status"]),
    "task-status": _t(
        "Check a mesh task's status.",
        "Reads the task state from the gateway; `--refresh` asks the peer "
        "again instead of using the cached view.",
        "neuralosd mesh task-status TASK_ID [--refresh]",
        ["neuralosd mesh task-status T-91 --refresh"],
        exits="0 status printed; 1 unknown task",
        see=["mesh.task-create"]),
    "task-cancel": _t(
        "Cancel a mesh task.",
        "Requests cancellation of an in-flight task on its peer.",
        "neuralosd mesh task-cancel TASK_ID",
        ["neuralosd mesh task-cancel T-91"],
        exits="0 cancelled; 1 unknown/terminal task",
        safety="Stops work on the peer.",
        see=["mesh.task-status"], mutates=True),
    "task-retry": _t(
        "Retry a failed mesh task.",
        "Re-queues a failed task with its original payload.",
        "neuralosd mesh task-retry TASK_ID",
        ["neuralosd mesh task-retry T-91"],
        exits="0 re-queued; 1 task not retryable",
        see=["mesh.task-status"], mutates=True),
    "mailbox": _t(
        "Read a peer's mailbox.",
        "Reads mailbox messages for a peer, filtered by skill (default "
        "status). It does not send: use `mesh send` for that.",
        "neuralosd mesh mailbox --target PEER [--skill NAME]",
        ["neuralosd mesh mailbox --target finance-edge",
         "neuralosd mesh mailbox --target finance-edge --skill report"],
        exits="0 ok; 1 gateway error",
        see=["mesh.send"]),
    "trust": _t(
        "List the gateway's peer trust state.",
        "Prints the trust records the gateway holds for known peers "
        "(identities and pinned fingerprints). It reads; it does not "
        "change the local trust store.",
        "neuralosd mesh trust",
        ["neuralosd mesh trust"],
        exits="0 printed; 1 gateway unreachable",
        see=["mesh.verify"]),
    "setup": _t(
        "Configure mesh credentials and check reachability.",
        "Stores the gateway credential (argument or the configured env file) "
        "where the client expects it, then verifies the gateway answers.",
        "neuralosd mesh setup [--token VALUE] [--token-path PATH]",
        ["neuralosd mesh setup --token-path "
         "$HOME/.config/reactorpro/gateway.env"],
        exits="0 ready; 1 unreachable/unauthorised",
        safety="Writes the gateway credential file.",
        see=["mesh.health"], mutates=True),
    "gossip-push": _t(
        "Publish this instance's gap fingerprints to peers.",
        "Exports refusal fingerprints (hashes, not questions) from a "
        "pre-exported file or the audit log, and sends them to the peers "
        "you name.",
        "neuralosd mesh gossip-push --peers A,B "
        "(--fingerprints FILE | --audit FILE)",
        ["neuralosd mesh gossip-push --peers finance-edge "
         "--audit ./global-sales/ask_audit.jsonl"],
        exits="0 sent; 2 no fingerprints source; 1 gateway error",
        see=["gaps"], mutates=True),
    "gossip-pull": _t(
        "Collect peers' gap fingerprints.",
        "Reads the gossip inbox directory the gateway writes and merges "
        "peer fingerprints into the local set.",
        "neuralosd mesh gossip-pull --inbox DIR",
        ["neuralosd mesh gossip-pull --inbox ./gossip_inbox"],
        exits="0 merged; 1 unreadable inbox",
        see=["mesh.gossip-push", "mesh.gaps"], mutates=True),
    "preflight": _t(
        "Would the gate answer this question? (no probe runs)",
        "Runs routing only and reports the chosen probe and score, or the "
        "refusal reason — side-effect free.",
        "neuralosd mesh preflight --instance-dir DIR QUESTION...",
        ["neuralosd mesh preflight 'what is our revenue' "
         "--instance-dir ./global-sales"],
        exits="0 would answer; 2 would refuse",
        see=["ask"]),
    "gaps": _t(
        "Cluster local + gossiped fingerprints into proposals.",
        "Combines this instance's gaps with peers' fingerprints and emits "
        "clustered authoring proposals.",
        "neuralosd mesh gaps --instance-dir DIR [--min-count N] [--json]",
        ["neuralosd mesh gaps --instance-dir ./global-sales"],
        exits="0 report; 1 unreadable ledger",
        see=["gaps", "mesh.gossip-pull"]),
    "verify": _t(
        "Verify a signed mesh envelope.",
        "Checks an envelope file's signature and menu/gate versions against "
        "the supplied key or the pinned peer identity.",
        "neuralosd mesh verify --file ENVELOPE.json [--key KEY]",
        ["neuralosd mesh verify --file envelope.json"],
        exits="0 valid; 1 invalid/unknown signer",
        see=["mesh.trust"]),
    "health": _t(
        "Check the Synapse gateway.",
        "Reports gateway reachability, auth and the local peer registration.",
        "neuralosd mesh health", ["neuralosd mesh health"],
        exits="0 healthy; 1 unreachable/unauthorised",
        see=["mesh.setup"]),
    "register": _t(
        "Register this instance on the mesh.",
        "Publishes the instance's identity and capabilities to the gateway; "
        "`--publish` advertises probes as skills immediately.",
        "neuralosd mesh register --instance-dir DIR [--publish] [--json]",
        ["neuralosd mesh register --instance-dir ./global-sales --publish "
         "--json"],
        exits="0 registered; 1 gateway error",
        see=["mesh.serve"], mutates=True),
}


PLASMA = {
    "catalog": _t(
        "List Plasma commands with their safety class.",
        "Reads `plasma help --json` and prints every command path, its group "
        "and safety (read-only / plan-gated / mutating). `--read-only` and "
        "`--plan-gated` filter; `--group` limits to one Plasma group.",
        "neuralosd plasma catalog [--read-only|--plan-gated] [--group G] "
        "[--json]",
        ["neuralosd plasma catalog --read-only",
         "neuralosd plasma catalog --plan-gated --json"],
        exits="0 printed; 1 Plasma missing or catalog invalid",
        see=["plasma.init"]),
    "init": _t(
        "Scaffold a neuralosd instance from the Plasma catalog.",
        "Saves the catalog and writes a probes.py that exposes every "
        "generatable read-only Plasma command as a typed probe, with the "
        "state root and event store baked in. The instance loads without "
        "Plasma running; only probe execution calls the binary.",
        "neuralosd plasma init --out DIR [--root PATH] [--events PATH] "
        "[--group G] [--include 'command path']",
        ["neuralosd plasma init --out ./plasma-estate --root .plasma",
         "neuralosd plasma init --out ./plasma-ro --group health --group "
         "trust"],
        exits="0 written; 1 Plasma missing or catalog invalid",
        safety="Writes an instance directory (never touches Plasma state).",
        see=["plasma.probes", "ask"], mutates=True),
    "probes": _t(
        "List the probes the catalog would generate.",
        "Shows probe names, triggers and typed arguments without writing an "
        "instance. Useful before `init` and for auditing routing coverage.",
        "neuralosd plasma probes [--group G] [--include PATH] [--json]",
        ["neuralosd plasma probes",
         "neuralosd plasma probes --json"],
        exits="0 printed; 1 Plasma missing",
        see=["plasma.init"]),
    "call": _t(
        "Run one READ-ONLY Plasma command.",
        "Executes exactly one catalog command (argv list, no shell) with "
        "typed `--param key=value` options and prints its JSON. Mutating and "
        "plan-gated commands are refused: use plan/apply.",
        "neuralosd plasma call COMMAND... [--param k=v] [--root P] "
        "[--events P]",
        ["neuralosd plasma call health",
         "neuralosd plasma call control audit tail --param limit=20"],
        exits="0 ok; 1 command failed; 2 not read-only",
        see=["plasma.plan"]),
    "plan": _t(
        "Dry-run one plan-gated command and record the plan.",
        "Validates the command and parameters against the catalog, runs "
        "Plasma's own dry-run, and records a local plan id plus the Plasma "
        "plan/approval id when it returns one. Nothing is executed.",
        "neuralosd plasma plan COMMAND... [--param k=v] [--root P]",
        ["neuralosd plasma plan 'enforce run'",
         "neuralosd plasma plan 'prevent rollback' --param id=12"],
        exits="0 planned; 1 not plan-gated or Plasma failed",
        safety="Dry-run only; `apply` performs the action.",
        see=["plasma.apply"]),
    "apply": _t(
        "Execute a recorded Plasma plan.",
        "Runs the recorded command with `--apply` (and `--plan <id>` when "
        "Plasma returned one). A plan applies once; a second attempt is "
        "refused. The command must still exist and still be plan-gated.",
        "neuralosd plasma apply PLAN_ID [--root P]",
        ["neuralosd plasma apply plp_0123456789ab"],
        exits="0 applied; 1 unknown/already-applied/failed",
        safety="Performs Plasma's plan-gated action on the host.",
        see=["plasma.plan"], mutates=True),
    "pending": _t(
        "List containment decisions awaiting approval.",
        "Shortcut for `plasma call enforce pending`.",
        "neuralosd plasma pending [--root P] [--json]",
        ["neuralosd plasma pending"],
        exits="0 ok; 1 Plasma failed",
        see=["plasma.call"]),
    "health": _t(
        "Plasma health verdict.",
        "Shortcut for `plasma call health`: a deterministic verdict over the "
        "local Plasma stack with blind-spot accounting.",
        "neuralosd plasma health [--root P] [--json]",
        ["neuralosd plasma health"],
        exits="0 ok; 1 Plasma failed",
        see=["plasma.call"]),
}


DOORS = {
    "list": _t(
        "List deployed doors.", "Shows every recorded door: name, backend, "
        "host port, target port and liveness.",
        "neuralosd door list [--json]", ["neuralosd door list --json"],
        exits="0 always", see=["deploy"]),
    "stop": _t(
        "Stop a door's sandbox and forget it.",
        "Terminates the sandbox behind the named door and removes its record.",
        "neuralosd door stop NAME", ["neuralosd door stop sales-prod"],
        exits="0 stopped; 1 unknown door",
        safety="Stops the running sandbox.", see=["deploy"], mutates=True),
    "proxy": _t(
        "Add a host relay for a sandbox without a native forward.",
        "Runs a host-side TCP relay from a host port to the sandbox port; "
        "needed for Microsandbox, whose forwards are create-time only.",
        "neuralosd door proxy --name NAME --port P [--target T] [--host H] "
        "[--backend msb|boxlite]",
        ["neuralosd door proxy --name sales-prod --port 8501 --target 8420 "
         "--host 0.0.0.0"],
        exits="0 relay running; 1 unknown door/port busy",
        safety="Opens a host port.", see=["deploy"], mutates=True),
}


# ── rendering ────────────────────────────────────────────────────────────

def lookup(topic):
    """The catalogue entry for ``name`` or ``group.name``, or None."""
    if topic in TOPICS:
        return TOPICS[topic]
    if "." in topic:
        group, name = topic.split(".", 1)
        table = {"mesh": MESH, "door": DOORS, "plasma": PLASMA}.get(group)
        if table and name in table:
            return table[name]
    return None


def _footer(entry):
    lines = []
    if entry.get("exits"):
        lines.append("Exit codes: " + entry["exits"])
    if entry.get("safety"):
        lines.append("Safety: " + entry["safety"])
    if entry.get("see"):
        lines.append("See also: " + ", ".join(entry["see"]))
    return "\n".join(lines)


def description(topic):
    entry = lookup(topic)
    if not entry:
        return None
    parts = [entry["description"]]
    if entry.get("usage"):
        parts.append("Usage:\n  " + entry["usage"])
    return "\n\n".join(parts)


def epilog(topic):
    entry = lookup(topic)
    if not entry:
        return None
    lines = ["Examples:"]
    lines += ["  " + ex for ex in entry["examples"]]
    footer = _footer(entry)
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def main_description():
    return (
        "neuralOSd — deterministic-first agentic runtime.\n\n"
        "Ask questions through a closed probe menu; get a verified answer "
        "or an explicit refusal. Serve instances over HTTP, verify banks, "
        "author new probes, and run sandboxes.\n\n" + CONTRACT)


def main_epilog():
    return """\
Examples:
  neuralosd init --source ./sales.csv --name sales
  neuralosd ask --instance-dir ./sales "total revenue by region"
  neuralosd serve --instance-dir ./sales --port 9998 --api-key "$KEY"
  neuralosd golden --dir ./sales
  neuralosd help ask                 # full help for one command
  neuralosd help --json              # machine-readable catalogue

Every subcommand accepts -h/--help. `neuralosd help <command>` prints one
command's full page; `neuralosd help --json` includes every command's usage,
examples, flags and defaults for orchestrating agents."""


def render_index(stream=None):
    stream = stream or sys.stdout
    print("neuralosd %s — command catalogue\n" % __version__, file=stream)
    print("Use `neuralosd help <command>` or `<command> --help` for details.\n",
          file=stream)
    groups = [
        ("Create & deploy", ["init", "deploy", "door", "backends", "sidecar"]),
        ("Ask & serve", ["ask", "serve", "reason", "browser"]),
        ("Author capabilities", ["build", "pi", "temporal", "gaps", "synonyms",
                                 "transfer", "shadow", "feedback", "mothball",
                                 "drift", "snapshot"]),
        ("Verify & observe", ["golden", "truth", "invariants", "calibrate",
                              "lint", "openapi", "diff", "slo", "fleet",
                              "badge", "doctor", "audit"]),
        ("Act & watch", ["actuate", "watch", "roles", "scrub"]),
        ("Plasma (EDR)", ["plasma"]),
        ("Mesh (Synapse)", ["mesh"]),
        ("Meta", ["docs", "version", "update", "help"]),
    ]
    for title, names in groups:
        print(title, file=stream)
        for name in names:
            entry = TOPICS.get(name)
            if entry:
                print("  %-12s %s" % (name, entry["summary"]), file=stream)
        print(file=stream)
    print("Every command: `neuralosd <command> --help`.", file=stream)
    print("Machine-readable: `neuralosd help --json`.", file=stream)


# ── attaching text to parsers ────────────────────────────────────────────

def _attach(parser, topic):
    entry = lookup(topic)
    if not entry:
        return
    parser.description = description(topic)
    parser.epilog = epilog(topic)
    parser.formatter_class = argparse.RawDescriptionHelpFormatter


def _relabel(choices, actions, prefix=""):
    """Replace argparse's one-line subcommand help with our summaries."""
    by_dest = {}
    for action in actions:
        dest = getattr(action, "dest", None)
        if dest:
            by_dest[dest] = action
    for name, parser in choices.items():
        entry = lookup(prefix + name)
        if entry and name in by_dest:
            by_dest[name].help = entry["summary"]
        for action in getattr(parser, "_actions", []):
            if isinstance(action, argparse._SubParsersAction):
                _relabel(action.choices,
                         getattr(action, "_choices_actions", []),
                         "%s%s." % (prefix, name))


def install(subparsers):
    """Attach full text to every built parser, including mesh/door groups."""
    for name, parser in getattr(subparsers, "choices", {}).items():
        _attach(parser, name)
        for action in getattr(parser, "_actions", []):
            if isinstance(action, argparse._SubParsersAction):
                for subname, subparser in action.choices.items():
                    _attach(subparser, "%s.%s" % (name, subname))
    _relabel(getattr(subparsers, "choices", {}),
             getattr(subparsers, "_choices_actions", []))


# ── machine-readable catalogue ───────────────────────────────────────────

def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    return str(value)


def _flags(parser):
    out = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction,
                               argparse._SubParsersAction)):
            continue
        item = {
            "flags": list(action.option_strings),
            "dest": action.dest,
            "help": action.help,
            "required": bool(getattr(action, "required", False)),
            "default": _plain(action.default),
        }
        if action.choices:
            item["choices"] = [str(c) for c in action.choices]
        if getattr(action, "nargs", None) is not None:
            item["nargs"] = str(action.nargs)
        out.append(item)
    return out


def catalogue(parser=None):
    """The full machine-readable help document."""
    doc = {
        "name": "neuralosd",
        "version": __version__,
        "contract": CONTRACT.strip().splitlines(),
        "commands": {},
        "groups": {},
    }
    if parser is not None:
        subs = next((a for a in parser._actions
                     if isinstance(a, argparse._SubParsersAction)), None)
        choices = getattr(subs, "choices", {})
    else:
        choices = {}
    for name, entry in TOPICS.items():
        item = {k: v for k, v in entry.items() if k not in ("mutates",)}
        item["mutates"] = entry["mutates"]
        p = choices.get(name)
        if p is not None:
            item["flags"] = _flags(p)
        doc["commands"][name] = item
    for group, table in (("mesh", MESH), ("door", DOORS),
                        ("plasma", PLASMA)):
        group_doc = {"summary": TOPICS[group]["summary"], "commands": {}}
        gp = choices.get(group)
        subchoices = {}
        if gp is not None:
            sa = next((a for a in gp._actions
                       if isinstance(a, argparse._SubParsersAction)), None)
            subchoices = getattr(sa, "choices", {})
        for name, entry in table.items():
            item = {k: v for k, v in entry.items() if k not in ("mutates",)}
            item["mutates"] = entry["mutates"]
            p = subchoices.get(name)
            if p is not None:
                item["flags"] = _flags(p)
            group_doc["commands"][name] = item
        doc["groups"][group] = group_doc
    return doc


# ── the `neuralosd help` command ─────────────────────────────────────────

def run(a):
    from .cli import build_parser      # lazy: cli imports this module

    ap = build_parser()
    topic = getattr(a, "topic", None)
    as_json = bool(getattr(a, "json", False))

    if as_json:
        doc = catalogue(ap)
        if topic:
            entry = doc["commands"].get(topic)
            if entry is None and "." in topic:
                group, name = topic.split(".", 1)
                entry = doc["groups"].get(group, {}).get(
                    "commands", {}).get(name)
            if entry is None:
                print(f"error: unknown command: {topic}", file=sys.stderr)
                return 2
            doc = {"name": "neuralosd", "version": __version__,
                   "command": topic, "help": entry}
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
        return 0

    if not topic:
        render_index()
        return 0

    subs = next((act for act in ap._actions
                 if isinstance(act, argparse._SubParsersAction)), None)
    choices = getattr(subs, "choices", {})
    if topic in choices:
        choices[topic].print_help()
        return 0
    if "." in topic:
        group, name = topic.split(".", 1)
        gp = choices.get(group)
        if gp is not None:
            sa = next((act for act in gp._actions
                       if isinstance(act, argparse._SubParsersAction)), None)
            if sa is not None and name in sa.choices:
                sa.choices[name].print_help()
                return 0
    print(f"error: unknown command: {topic}", file=sys.stderr)
    return 2
