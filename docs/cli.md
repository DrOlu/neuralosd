# CLI reference — every subcommand

`neuralosd` ships **42 top-level subcommands**, a `mesh` group with **20 subcommands**, and a `door` group with **3 subcommands**. This page is the complete reference: one entry per command, its key flags, and a runnable example. For guided tutorials see [usage](usage.md); for the Python/HTTP API see [reference](reference.md); for end-to-end recipes see the [cookbook](cookbook.md).

Conventions used everywhere below:

- **`--instance-dir DIR`** — the instance directory (contains `needle_menu.json`, `banks/`, `ask_audit.jsonl`). Most commands infer it from `./` if omitted. Verification commands (`golden`, `truth`, `invariants`, `calibrate`) historically use **`--dir`** instead — same meaning.
- **Exit codes**: `0` = answered/verified, `2` = refused (a JSON envelope with `error` + `refusal_reason` is printed), other non-zero = operational failure. Applies to `ask`, `serve` health probes, and the verification commands.
- **Arguments are never coerced** — a probe declared `int` will not accept `"42"`; the gate refuses instead of guessing.

## Command map

| Lifecycle | Commands |
|---|---|
| **Create & deploy** | `init` · `deploy` · `door` (`list`/`stop`/`proxy`) · `backends` · `sidecar` |
| **Ask & serve** | `ask` · `serve` · `reason` · `browser` |
| **Author capabilities** | `build` · `pi` · `temporal` · `gaps` · `synonyms` · `transfer` · `shadow` · `feedback` · `mothball` · `drift` · `snapshot` |
| **Verify & observe** | `golden` · `truth` · `invariants` · `calibrate` · `lint` · `openapi` · `diff` · `slo` · `fleet` · `badge` · `doctor` · `audit` |
| **Act & watch** | `actuate` · `watch` · `roles` · `scrub` |
| **Plasma (EDR)** | `plasma` (`catalog`/`init`/`probes`/`call`/`plan`/`apply`/`pending`/`health`) |
| **Mesh (Synapse)** | `mesh serve` · `register` · `discover` · `agents` · `ask` · `delegate` · `send` · `task-create/status/cancel/retry` · `mailbox` · `trust` · `setup` · `gossip-push` · `gossip-pull` · `preflight` · `gaps` · `verify` · `health` |
| **Meta** | `docs` · `version` · `update` · `help` |

---

## Create & deploy

### `init` — scaffold a new instance from a data source

```sh
neuralosd init --source ./sales_2024.csv --name global-sales
neuralosd init --source postgresql://user:pass@host/analytics --name fin --packages pandas
neuralosd init --source syslog:/var/log/system.log --name syslog --header "X-Tenant: acme" --secret-ref pg_password
```

| Flag | Purpose |
|---|---|
| `--source` | CSV path, DB DSN, JSON, log path, or API URL (repeatable: multi-source instances) |
| `--name` | Instance name (used in service mode + mesh) |
| `--out` | Output directory (default `./<name>`) |
| `--extra-source` | Append additional sources after scaffolding |
| `--header K: V` | Inject a header for API sources |
| `--secret-ref NAME` | Reference a secret by name instead of embedding it |
| `--since` / `--on` | Time-window hints for log/API sources |
| `--packages` | Extra pip packages the generated probes may import |

What lands in the instance dir: a **profile** of the data, **Pydantic models**, a **router**, a starter **probe menu**, and the **banks** directory (golden questions, truth oracles, invariants). See [usage §3](usage.md#3-build-an-instance).

### `deploy` — deploy the instance into a sandbox

```sh
neuralosd deploy --instance-dir ./global-sales --backend boxlite --name sales-prod --port 8501
neuralosd deploy --instance-dir ./global-sales --name sales-2 --backend msb --from-template sales-gold
neuralosd deploy --instance-dir ./global-sales --name sales-gold --save-template sales-gold --force
```

| Flag | Purpose |
|---|---|
| `--backend` | `boxlite` (recommended) or `msb` (Microsandbox); default lists via `backends` |
| `--name` / `--port` / `--bind` | Sandbox name, host port, bind address |
| `--packages` / `--index-url` | Extra packages inside the sandbox (custom index for air-gapped hosts) |
| `--from-template` | Clone a saved instance template |
| `--save-template` | Snapshot this instance as a reusable template |
| `--force` | Replace an existing sandbox of the same name |

### `door` — manage deployed instance doors (host → sandbox)

```sh
neuralosd door list --json
neuralosd door stop sales-prod
neuralosd door proxy --name sales-prod --port 8501 --target 8420 --host 0.0.0.0
```

- **`door list`** — every registered door with its backend, ports and health (`--json` for machine-readable).
- **`door stop NAME`** — stop the sandbox and forget the registration.
- **`door proxy`** — expose the sandbox's service port on the host (`--name`, `--port`, `--target`, `--host`, `--backend`, `--instance-dir`).

### `backends` — list available sandbox backends

```sh
neuralosd backends
```

Prints which of BoxLite / Microsandbox are installed and usable on this host, with hints for missing ones.

### `sidecar` — provision a sidecar environment

```sh
neuralosd sidecar --python /usr/bin/python3 --force
```

Single-file binaries can't carry every scientific library. The sidecar provisions a Python environment so the binary can use **host libraries** (pandas, numpy, …) without a sandbox. `--force` rebuilds; the binary detects and uses it automatically.

---

## Ask & serve

### `ask` — ask a question

```sh
neuralosd ask --instance-dir ./global-sales "total revenue by region"
neuralosd ask --instance-dir ./syslog --role oncall --as-of 2026-12-01 "failed logins on ws1"
neuralosd ask --strict --instance-dir ./fin "net margin per quarter"
```

| Flag | Purpose |
|---|---|
| `--model` | Load the on-device model as a routing fallback |
| `--strict` | Reject answers that fall back to partial menus |
| `--explain` | Dry run: show the probe, arguments and gate decision without executing or auditing |
| `--as-of ISO` | Point temporal questions at a specific date |
| `--role ROLE` | Ask under an `acl.json` role (allow-list + row filters apply) |

**Exit `0`** with populated `results`, or **exit `2`** with `results: null` and a typed `refusal_reason` (`no_probe_matches`, `dropped_filter`, `arg_type_mismatch`, …). A refusal is an answer, not an error — every refusal is journaled to `ask_audit.jsonl` and can become a roadmap item via `gaps`. See [usage §6](usage.md#6-ask-questions).

### `serve` — start the authenticated HTTP API

```sh
neuralosd serve --instance-dir ./global-sales --port 9998 --api-key "$KEY"
neuralosd serve --instance-dir ./a --instance-dir ./b --port 9998 \
    --read-only --tls-cert server.crt --tls-key server.key
```

| Flag | Purpose |
|---|---|
| `--instance-dir` | Instance directory (repeatable; directory name = instance name) |
| `--instances-dir` | Legacy multi-instance door (kept for compatibility) |
| `--api-key` / `--api-key-file` | API key(s); default lab key `neuralosd` |
| `--read-only` | Refuse apply/undo/reload (reads and dry-runs remain) |
| `--tls-cert` / `--tls-key` | Wrap the listener in TLS (both required) |
| `--timeout`, `--max-body`, `--max-output`, `--plan-ttl` | Limits |
| `--audit/--no-audit` | API audit chain (default on) |
| `--allow-mesh` | Enable the mesh discover/preflight/delegate endpoints (off by default) |
| `--host` / `--port` | Bind address (default `127.0.0.1:9998`) |
| `--model` / `--strict` | Same semantics as `ask` |

The `/v1` surface, auth, OpenAPI 3.1 at `/openapi.json`, `/docs`, plans,
audit and deployment notes are in the [HTTP API guide](api.md). A
non-loopback bind is refused unless a non-default key is configured.

### `reason` — escalate to a reasoning model, then extend the menu

```sh
neuralosd reason --instance-dir ./global-sales "why did EMEA margins dip in Q3" --consensus-model qwen3:8b
neuralosd reason --shadow --instance-dir ./global-sales "…"          # draft, don't register
```

Runs the multi-step reasoning loop when no probe can answer: proposes derived metrics, optionally reaches consensus across models, verifies against the truth oracle, and (without `--shadow`) registers what passes. Flags: `--model`, `--ollama URL`, `--oracle`, `--scale ratio|percent`, `--consensus-model`, `--shadow`.

### `browser` — drive a named agent-browser session

```sh
neuralosd browser open --url https://status.internal.example --session ops
neuralosd browser snapshot --session ops
neuralosd browser click --target e12 --session ops
neuralosd browser fill --target e7 --text "acme" --session ops
```

Subcommands: `open`, `snapshot`, `click`, `fill`, `type`, `network`, `text`, `setup`. Lets a probe read data that only exists behind a login or a JS-rendered page.

---

## Author capabilities

### `build` — natural language in, VERIFIED capability out

```sh
neuralosd build --instance-dir ./global-sales "shipping cost per unit by region"
neuralosd build --instance-dir ./global-sales --durable "…"         # survives crashes/reboots
neuralosd build --instance-dir ./global-sales --durable --resume    # resume an interrupted build
neuralosd build --instance-dir ./global-sales --provider superagent --timeout 1800 "…"
```

Delegates to the **pi coding agent**: pi profiles the data, writes the probe + triggers + Pydantic args, and the **verification gate** decides. The gate merges only clean work — golden bank and truth oracle must stay **byte-identical**, all questions must replay green — otherwise the instance is auto-restored to its pre-build state. Flags: `--provider` (pi provider; see `NEURALOSD_PI_PROVIDER`), `--session` (inspect/steer an existing session), `--timeout`, `--durable`, `--resume`.

### `pi` — drive the pi coding agent (operator mode + setup)

```sh
neuralosd pi --instance-dir ./global-sales "inspect the banks layout"
neuralosd pi --instance-dir ./global-sales --durable --status       # durable run status
neuralosd pi --instance-dir ./global-sales --steer "use polars"     # steer a running build
neuralosd pi --instance-dir ./global-sales --stop                   # stop the durable run
```

One-shot operator sessions over the same staged bridge `build` uses. Flags: `--model`, `--provider`, `--session`, `--timeout`, `--durable`, `--status`, `--steer MESSAGE`, `--stop`.

### `temporal` — durable execution via Temporal

```sh
neuralosd temporal --setup                                    # check CLI + server + SDK
neuralosd temporal --worker --instance-dir ./global-sales     # run a durable worker
neuralosd temporal --run-activity loop_activity --instance-dir ./global-sales
neuralosd temporal --schedule-create --instance-dir ./global-sales --cron "0 * * * *"
```

For teams standardised on Temporal: runs the self-test/loop as durable workflow activities with schedules. See also the **Restate** route in [usage §6c](usage.md#6c-web-apis-and-trust-v240) if you prefer single-binary durable execution.

### `gaps` — mine gated/fuzzy questions into proposals

```sh
neuralosd gaps ./global-sales/ask_audit.jsonl --menu ./global-sales/needle_menu.json
neuralosd gaps ./global-sales/ask_audit.jsonl --export-fingerprints ./fps.json
neuralosd gaps ./global-sales/ask_audit.jsonl --ingest-fingerprints ./peers.json   # mesh gossip import
```

Clusters every refusal and near-miss into **trigger proposals**: missing synonyms (Type A) and genuinely new probes (Type B). `--export-fingerprints`/`--ingest-fingerprints` feed the mesh gossip loop.

### `synonyms` — propose (and apply) trigger synonyms

```sh
neuralosd synonyms --instance-dir ./global-sales               # print proposals
neuralosd synonyms --instance-dir ./global-sales --apply proposals.json
```

The human-gated half of gap mining: reviews the proposed trigger additions, then applies exactly the approved set.

### `transfer` — propose a schema-sibling's probes for a new source

```sh
neuralosd transfer --from ./sales_2024.csv --to ./sales_2025.csv --min-sim 0.6 --out transfer.json
```

Profiles both sources, finds schema siblings, and translates the golden bank so a proven instance can be re-validated against a new file/database with minimal rewriting.

### `shadow` — canary new probes without serving them

```sh
neuralosd shadow --instance-dir ./global-sales                 # report shadowed probes
neuralosd shadow --instance-dir ./global-sales --evaluate      # one agreement pass vs oracles
neuralosd shadow --instance-dir ./global-sales --promote --min-agreement 0.98 --min-runs 20
```

Shadowed probes run on real traffic but their answers are only **recorded, never served**, until agreement with the oracles clears your thresholds.

### `feedback` — turn a rejected answer into a golden/trap candidate

```sh
neuralosd feedback --instance-dir ./global-sales \
  --question "revenue by region" --verdict bad \
  --correction "exclude intercompany" --by ada --apply
```

The operator feedback loop: records who flagged what, appends to the banks as golden corrections or traps (questions that must keep refusing), `--apply` writes it into the instance.

### `mothball` — demote probes nobody queries (kept, reinstatable)

```sh
neuralosd mothball --instance-dir ./global-sales --age-days 90          # report candidates
neuralosd mothball --instance-dir ./global-sales --age-days 90 --apply  # demote them
neuralosd mothball --instance-dir ./global-sales --reinstate probe_id   # bring one back
```

Shrinks the menu without deleting work — mothballed probes stay on disk and leave no trace in the gate except absence.

### `drift` — re-audit against a changed source schema

```sh
neuralosd drift --instance-dir ./global-sales --source ./sales_2025.xlsx
```

The source changed under a live instance: re-profiles, diffs columns/semantics, and reports which probes/oracles are affected before anything breaks.

### `snapshot` — pin a hashed snapshot of every probe observation

```sh
neuralosd snapshot --instance-dir ./global-sales --label pre-migration-2026-12
```

Cryptographic baselines for every probe's current observation — the reference point `diff`, `drift` and audits compare against.

---

## Verify & observe

### `golden` — run the golden question bank

```sh
neuralosd golden --dir ./global-sales
neuralosd golden --dir ./global-sales --question "total revenue" --expect-probe revenue_by_region
neuralosd golden --dir ./global-sales --question "…" --expect-refusal --value null
neuralosd golden --dir ./global-sales --receipts --audit --by ada --source "PR #123 review"
```

Every golden question must route to its probe and return the recorded value; `--expect-refusal` pins questions that must keep refusing (traps). `--record` adds new verified Q/A pairs; `--receipts` prints who verified what; `--audit` writes the receipt into the bank.

### `truth` — run the truth oracle

```sh
neuralosd truth --dir ./global-sales --audit ./global-sales/ask_audit.selftest.jsonl
```

Recomputes every probe's expected values from the source of truth — the oracle that makes "banks stay byte-identical" enforceable. Non-zero exit on any mismatch.

### `invariants` — run property checks

```sh
neuralosd invariants --dir ./global-sales
```

Property-based checks that must hold for every observation (non-negative totals, referential integrity, `lte_1` expectations for ratio metrics, …).

### `calibrate` — learn per-probe confidence gates

```sh
neuralosd calibrate --golden ./global-sales/golden.json --traps ./global-sales/traps.json
neuralosd calibrate --golden ./global-sales/golden.json --allow-no-traps
```

Fits the per-probe confidence thresholds from golden passes and trap refusals, so the gate admits answers and rejects near-misses at the right per-probe sensitivity.

### `lint` — trigger collision linter

```sh
neuralosd lint ./global-sales/needle_menu.json --top 10
```

Finds trigger phrases that would route to the wrong probe (collisions between menus/probes) before they become silent misroutes.

### `openapi` — generate OpenAPI + MCP manifest

```sh
neuralosd openapi ./global-sales/needle_menu.json --agent global-sales
```

Emits an OpenAPI spec of the probe surface and an MCP manifest so any MCP client can call the instance's probes as typed tools.

### `diff` — profile schema-evolution diff

```sh
neuralosd diff ./old.csv ./new.csv --menu ./global-sales/needle_menu.json
```

Compares two profiles (columns, types, semantics) and reports what the instance's probes would notice.

### `slo` — refusal-rate SLO for one instance

```sh
neuralosd slo --instance-dir ./global-sales --window-hours 168 --max-refusal-rate 0.15
```

Computes the honest refusal-rate SLO over the audit log window; non-zero exit when over budget. Deliberately excludes **dated refusals** (records whose timestamp fell outside the question's time range) from the denominator — those are correct behaviour, not misses.

### `fleet` — multi-instance health dashboard

```sh
neuralosd fleet ~/instances/finance ~/instances/sales ~/instances/syslog --max-refusal-rate 0.2
```

One table across many instance dirs: menu/probe counts, refusal SLOs, bank health, last-build age. Non-zero exit if any instance breaches its budget — CI-friendly.

### `badge` — golden-bank health badge + GitHub Actions snippet

```sh
neuralosd badge --instance-dir ./global-sales --actions-out .github/workflows/golden-badge.yml
```

Generates a shields-style JSON badge endpoint + the workflow snippet to keep it fresh on every push.

### `doctor` — health-check every integration

```sh
neuralosd doctor                          # host-level: engines, node, packages
neuralosd doctor --instance-dir ./global-sales --json
```

Checks each integration and says what to install when something is missing: `pi` (resolves the binary + pings it live), `pi_durable` (node + required npm packages + the packaged bridge), `browser`, `temporal`, `synapse`, `banks`. `--json` for machines.

### `audit` — classify instance imports

```sh
neuralosd audit --instance-dir ./global-sales
neuralosd audit --root ~/instances --json
```

For every import in every probe: **bundled** (safe), **host-only** (works only where the library exists — sidecar/sandbox), or **missing-everywhere** (will fail at ask time). Run it in CI before shipping an instance.

---

## Act & watch

### `actuate` — named, journaled, reversible action

```sh
neuralosd actuate --instance-dir ./security --action block_ip --target 10.0.0.42 \
  --reason "SSH brute force" --operator ada --dry-run
neuralosd actuate --instance-dir ./security --action block_ip --target 10.0.0.42 --apply
neuralosd actuate --instance-dir ./security --undo --undo-plan PLAN-8f3
```

The only sanctioned write path. Actions are a **closed list** (e.g. `block_ip`, `isolate_host`, `disable_user`); every run is **dry-run first** (`--plan` from a seen dry-run is required for `--apply`), journaled, and reversible (`--undo --undo-plan ID`). No arbitrary shell, ever.

### `watch` — stream sources into a bounded buffer; alert on rules

```sh
neuralosd watch --instance-dir ./syslog --source syslog:/var/log/auth.log \
  --rules rules.json --interval 5 --cap 5000
neuralosd watch --instance-dir ./syslog --rules rules.json --webhook https://ops.example/hook --once
```

Sources: `syslog:/path`, `journal` (repeatable). Rules match against the bounded ring buffer; alerts can fire **webhooks** (pipe them into an issue tracker and the loop closes itself). `--once` for one poll cycle (CI/cron friendly).

### `roles` — manage `acl.json` roles

```sh
neuralosd roles --instance-dir ./fin --op list
neuralosd roles --instance-dir ./fin --op set --role analyst --allow revenue_by_region,margin_by_quarter
neuralosd roles --instance-dir ./fin --op set --role analyst --row-filter 'region=in:EMEA'
neuralosd roles --instance-dir ./fin --op remove --role intern
```

Allow-list which probes a role may call and, optionally, apply row filters so `ask --role analyst` physically cannot see other regions' rows.

### `scrub` — sanitize cache + audit records already on disk

```sh
neuralosd scrub --instance-dir ./syslog
neuralosd scrub --instance-dir ./syslog --purge-cache
```

PII/secret hygiene after the fact: rewrites audit records against current scrubbing rules; `--purge-cache` drops the derived caches too.

---

## Mesh — Synapse integration

Most `mesh` subcommands accept `--gateway URL` (default: `$REACTORPRO_GATEWAY_URL` or `$NEURALOSD_SYNAPSE_URL` or `http://127.0.0.1:3000`) and `--timeout`. Gateways are pinned by identity/fingerprint (HMAC-SHA256 envelopes). Protocol version `1`.

```sh
# This edge answers mesh questions
neuralosd mesh serve --instance-dir ./global-sales --publish --heartbeat 30

# Find and call peers
neuralosd mesh discover --skill "revenue" --preflight
neuralosd mesh send --target finance-edge "total revenue by region"
neuralosd mesh delegate "q3 margin split by product line" --max-hops 2 --deadline-ms 60000

# Local router first, mesh when the local gate refuses
neuralosd mesh ask --instance-dir ./global-sales --delegate --preflight "…" 

# Durable async work + mailbox
neuralosd mesh task-create --target reporting-edge "build the nightly report"
neuralosd mesh task-status T-91 --refresh
neuralosd mesh task-retry T-91
neuralosd mesh mailbox --target finance-edge --skill report

# Trust + hygiene
neuralosd mesh trust                        # list the gateway's trust records
neuralosd mesh verify --file envelope.json
neuralosd mesh setup --token-path $HOME/.config/reactorpro/gateway.env   # credential + reachability check
neuralosd mesh health

# The gap loop, mesh-wide
neuralosd mesh register --instance-dir ./global-sales --publish --json
neuralosd mesh gossip-push --peers finance-edge --audit ./global-sales/ask_audit.jsonl   # publish my gap fingerprints
neuralosd mesh gossip-pull --inbox ./global-sales/gossip_inbox                            # collect peers' fingerprints
neuralosd mesh gaps --instance-dir ./global-sales           # cluster (mine + gossip) into proposals
neuralosd mesh preflight "…" --instance-dir ./global-sales  # would the gate answer? (no probe runs)
```

| Subcommand | One-line purpose |
|---|---|
| `serve` | Answer mesh questions for this instance (`--publish` to make it public) |
| `register` | Register the instance's capability menu on the gateway |
| `discover` | Find peers by skill/instance name (`--preflight` asks "would you answer this?") |
| `agents` | The peer directory (phonebook) with capabilities |
| `ask` | Local router first; `--delegate` forwards refusals to the best peer |
| `delegate` | Forward a question to the best peer, verified |
| `send` | Synchronous ask to one peer (dispatch/invoke) |
| `task-create/status/cancel/retry` | Durable async tasks on a peer — idempotent create, refreshable status, same-id retry |
| `mailbox` | Durable fire-and-forget message to a peer |
| `trust` | List the gateway's peer trust records (identities + fingerprints) |
| `verify` | Verify any signed envelope offline (stdin or `--file`) |
| `setup` | Check gateway token + reachability |
| `health` | Gateway liveness |
| `gossip-push` / `gossip-pull` | Share/collect gap fingerprints so peers learn what to build |
| `gaps` | Cluster refused questions (local + gossiped) into trigger proposals |
| `preflight` | Ask the gate "would you answer this?" without running a probe |

Trust classes (from `mesh trust` + gateway policy) keep strangers to **argument-types-only** envelopes; trusted peers get real args and durable tasks.

---

## Plasma — drive the EDR from neuralOSd

Plasma 1.24+ publishes its CLI as `plasma help --json`. neuralOSd consumes
that catalog: every generatable **read-only** command becomes a typed probe,
and plan-gated commands stay two-step. Full guide: [Plasma EDR](plasma.md).

```sh
neuralosd plasma catalog --read-only
neuralosd plasma init --out ./plasma-estate --root .plasma
neuralosd ask --instance-dir ./plasma-estate "is the estate healthy"
neuralosd plasma call health
neuralosd plasma plan "enforce run" --root .plasma
neuralosd plasma apply plp_0123456789ab --root .plasma
```

| Subcommand | Purpose |
|---|---|
| `catalog` | List Plasma commands, groups and safety classes |
| `init` | Scaffold an instance whose probes are the catalog's read-only commands |
| `probes` | Show the probes `init` would generate |
| `call` | Run one read-only command with validated `--param k=v` |
| `plan` | Dry-run a plan-gated command and record the plan |
| `apply` | Execute a recorded plan once (Plasma's `--apply --plan`) |
| `pending` | List containment decisions awaiting approval |
| `health` | Plasma health verdict |

---

## Meta

### `docs` — read the bundled documentation

```sh
neuralosd docs usage
neuralosd docs reference
```

The same docs shipped inside the binary — readable on an air-gapped host with no internet.

### `version` — what is installed

```sh
neuralosd version
neuralosd version --json          # {"version": "...", "install": "pip|frozen"}
neuralosd --version               # same, one line
```

### `update` — check for, or apply, a newer release

```sh
neuralosd update --check          # current → latest; changes nothing
neuralosd update --check --json   # suitable for a scheduler
neuralosd update                  # asks, then applies
neuralosd update --yes            # non-interactive
neuralosd update --channel pre    # include pre-releases
```

There is **no silent background updater**. A deterministic, offline-first
runtime must not call the network on import or on every ask, so the check
runs only when you run it.

How the update is applied depends on how neuralosd was installed:

- **pip / uv / pipx / uv tool** — the running interpreter's package manager
  upgrades the package (`pip install --upgrade neuralosd==<latest>`,
  `pipx upgrade neuralosd`, or `uv tool upgrade neuralosd`). An editable
  development checkout is refused: use `git pull` instead. Restart
  long-running services after the update.
- **Standalone binary** (the release assets and the npm launcher's download)
  — the matching asset for this platform is downloaded from the GitHub
  release, its **sha256 digest from the release metadata is verified**, the
  previous binary is kept as `<name>.bak`, and the verified file is swapped
  in atomically. A digest-less asset is refused. On Windows a running
  executable cannot be replaced, so the verified file is written as
  `<name>.new` and the next step is printed.
- **npm launcher** (`npm i -g neuralosd`) — update the launcher with
  `npm install -g neuralosd@latest`; the binary it downloads follows.

A scheduler can run the check daily and only alert on a new version:

```sh
neuralosd update --check --json | grep -q '"update_available": true' && echo "update available"
```

### `help` — the full help catalogue

```sh
neuralosd help                    # every command, grouped, with a one-line purpose
neuralosd help ask                # the same page as `neuralosd ask --help`
neuralosd help mesh.serve         # nested commands too
neuralosd help --json             # machine-readable catalogue
```

Every command's `-h/--help` is written for someone who has never seen the
repository: a full description, the usage line, runnable examples, exit
codes, safety notes (does it mutate state?), and related commands. The JSON
catalogue additionally exposes every flag with its dest, default, choices
and required marker, so an orchestrating agent can discover the CLI without
parsing prose.

---

## Global patterns

**Tokens & env.** `serve --auth-token` → `Authorization: Bearer` on HTTP; mesh auth comes from `REACTORPRO_GATEWAY_TOKEN` or the `gateway.env` file (`REACTORPRO_GATEWAY_ENV`, default `~/.config/reactorpro/gateway.env`). pi builds read `NEURALOSD_PI_PROVIDER` / `NEURALOSD_PI_MODEL` (flags override).

**Scripting.** Every command prints stable, parseable output and most offer a `--json` form; verification commands exit non-zero on failure so `set -e` pipelines just work:

```sh
neuralosd golden --dir ./global-sales && neuralosd slo --instance-dir ./global-sales || echo "bank or SLO regressed"
```

**The loop.** `ask` refusals → `gaps` clusters → `build` authors → `golden`/`truth` verify → the instance gets better without anyone guessing. That loop is the product.
