# Changelog

## 2.8.4 — release-pipeline hardening (gossip + watch, released)

- v2.8.3 never made it to PyPI: the release tag was created before
  pyproject.toml was bumped, so `python -m build` packaged 2.8.1 again
  while the sidecar test correctly refused to install a nonexistent
  neuralosd==2.8.3. pyproject and __init__ are now pinned equal by test,
  and the version-drift class is closed.
- Contents (built + verified on host + all 5 platforms via the v2.8.3
  CI unit runs): federation gossip protocol (push/collect/validate/merge)
  and streaming ingestion (`neuralosd watch`: bounded buffer, offset
  tails with rotation, threshold rules, webhooks).

## 2.8.3 — skipped (version drift, never published)

## 2.8.2 — gossip CLI import fix

- `neuralosd mesh gossip-push/pull` crashed on first real use:
  `from ..loop import` inside neuralosd.cli (a top-level module) escapes
  the package. Absolute imports + extracted helpers. Found by testing
  gossip-pull against a real converted instance on this host.

## 2.8.1 — federation gossip + streaming ingestion

- **Federation gossip (the protocol)** — `mesh gossip-push` publishes the
  local fingerprint blob to trusted peers via the gateway mailbox;
  `gossip-pull` collects and validates peer blobs (malformed fingerprints
  dropped, counts capped, deduped per peer); `gaps --federation-dir`
  reads the gossip state for mesh counts. Validation BEFORE send and on
  arrival: a misbehaving edge cannot poison or flood the mesh.
- **Streaming ingestion** — `neuralosd watch` tails syslog files /
  journald into a bounded buffer, evaluates threshold rules over sliding
  windows (per-cycle new events — no endless re-firing), and delivers
  alerts to webhooks. Offset-based tails with rotation detection.
- Docs: index + changelog for 2.8.1; smoke +1 watch cycle.

## 2.8.0 — joins for every source + roles management — joins for every source + roles management

- **All-source joins** — `--extra-source` accepts any init source (sqlite,
  xlsx, json, api with caged credentials, csv); the generated bridge
  dispatches per kind, and `--on name:key=key` joins deterministically.
  Fixes a silent CLI bug: `--extra-source` was dropped by a
  dest-name mismatch (singular vs plural) — found by the smoke suite,
  never caught by tests that called init.run directly.
- **Roles management** — `neuralosd roles` (set/list/show/remove) with
  live-menu validation, row-filter format checks, and a journal
  (acl_journal.jsonl). serve gains per-request roles via
  `X-NeuralOS-Role` (403 on unknown roles).
- **Bug**: the RBAC row cage filtered rows but left the probe's own count
  — the role saw "1 row" and "count: 2" at once. Caged views are now
  self-consistent.
- Smoke +6 (sqlite-extra joins, roles round-trip): 56 checks.

## 2.7.1 — smoke determinism

- The mothball smoke check asserted an anchor guess that flipped under
  Windows coarse clocks (same-second asks tie). It now asserts the
  platform-invariant behavior instead: the demoted probe refuses, the
  surviving anchor still answers. Windows binary run green.

## 2.7.0 — Pi Durable + doctor + loop optimizations

- **Pi Durable integration** — `neuralosd build "<capability>" --durable`
  runs the build conversation in a **Pi Durable** harness
  (@earendil-works/pi-durable): storage-backed (SQLite in the instance
  dir), crash-resumable, submissions exactly-once by a stable requestId
  (hash of instance+question). A crashed or interrupted build re-attaches
  with `--resume` — the in-flight reasoning survives. The same
  verification pipeline applies (banks untouched, instance loads, banks
  green, closure) — and on failure the files are restored while the
  conversation storage is KEPT for --resume. Ships
  `pi_durable_bridge.mjs` (line-JSON bridge over the Pi Durable API) and
  `neuralosd pi durable --status|--steer|--stop` operator ops.
- **`neuralosd doctor`** — one command that health-checks every
  integration (pi, Pi Durable, agent-browser, Temporal CLI+SDK, synapse
  token, openpyxl, the instance's own banks) with JSON output and an exit
  code. Exit 1 = something needs attention.
- **Builder prompt enrichment** — build prompts now carry the bridge's
  function inventory, existing probe names, and a data sample row up
  front: fewer exploration steps, higher first-shot success.
- **Bounded audit reads** — `slo`/`gaps`/`fleet` read audit logs with a
  line cap instead of loading multi-GB files whole.
- **Durability bug found by testing**: bank replays went through the ask
  cache — a "verification" could pass on a cached answer. Bank replays
  now always exercise the real code path.
- Tests: 969 passed (22 new incl. durable exactly-once, crash-resume,
  restore-on-failure, doctor), smoke 49.

## 2.6.1 — three bugs found testing pi on the real global-superstore instance

- **Bank replays went through the ask cache** — a "verification" could pass
  on a cached answer and never exercise the real code path. `golden_run`
  now bypasses the cache (the cache is for serving; replays verify).
- **Build gated on absolute bank health** — one stale auto-trap blocked
  every build forever. The gate now measures the DELTA: pre-existing
  failures are warnings, never attributed to the builder; only NEW
  failures block. The baseline replay runs before the backup snapshot
  (the baseline load births the auto-traps).
- **Stale-trap retraction** — `retract_stale_traps` removes
  auto-synthesized traps whose probe left the menu; human-written traps
  are never touched.

Found live: global-superstore's trap bank had 3 pre-existing failures
(breakdown hijacking "X values" phrasings for probes removed earlier).
With the fixes, the pi build delivered `unique_customers=1590` —
independently verified against the raw CSV — and a second build on the
rebuilt US superstore delivered `top_subcategory=Copiers` (55,617.82),
also independently verified.

## 2.6.0 — pi as builder and operator (out of the box)

- **`neuralosd build "<capability>" --instance-dir .`** — natural language
  in, VERIFIED capability out. The pi coding agent (headless:
  `--print --no-session`, scoped: extensions/MCP/skills/context-files off)
  works inside the instance directory and writes the probe code. Neuralosd
  then judges: the banks must be byte-identical, the instance must load,
  and both banks must replay green. Any failure — including pi tampering
  with traps.json or producing code that doesn't load — RESTORES the
  instance to its pre-pi state and reports honestly. Default model:
  `z-ai/glm-5.3-flash` (openrouter); override with `--model`/`--provider`
  or `$NEURALOSD_PI_MODEL`.
- **`neuralosd pi ask "<question>"`** — operator mode: pi investigates
  using the CLOSED LIST of neuralosd commands (gaps, slo, receipts,
  synonyms, drift); its runs are audited subprocesses in the instance dir;
  answers flagged as untrusted assistant output.
- **`neuralosd pi --setup`** — checks pi, resolves the model for the
  provider, and verifies with a live one-shot ping; unknown models point
  at `pi --list-models`.
- The v2.1.0 birth-trap contract holds for pi-built probes: the trap is
  banked at first load, proven by the conforming router.
- e2e-tested against the REAL pi + glm-5.3-flash: "build a max-units
  probe" → probe written, trap born, banks green, instance answers 200.
- Tests: 948 passed (19 new incl. the live e2e), smoke 49.

## 2.5.0 — synapse-gateway (for real) + Temporal durable execution

**Synapse-gateway — the REAL wire (bug fix + out-of-the-box messaging)**
- **Bug fix**: the previous client spoke INVENTED endpoints (`/discover`,
  `/request` on port 8090) that no real gateway answers. The transport now
  speaks the documented REST surface: `/api/mesh/agents`, `/api/mesh/dispatch`,
  `/api/mesh/tasks`, `/api/mesh/mailbox`, `/api/mesh/health|status|trust` —
  Bearer-authenticated, default gateway `http://127.0.0.1:3000`.
- **Token caging**: the token resolves from `REACTORPRO_GATEWAY_TOKEN` or the
  gateway.env file (`LIVEAGENT_GATEWAY_TOKEN=...`) at ask time — never stored
  in instance files.
- **Agent2agent messaging, out of the box**: `neuralosd mesh send --target
  <peer> "question"` (synchronous dispatch, reply read payload.text →
  payload.output, error envelopes carry the mesh code, replies flagged as
  UNTRUSTED REMOTE OUTPUT); durable tasks (`mesh task-create/--status
  --refresh/--cancel/--retry` — caller-minted idempotent taskId);
  `mesh mailbox` (fire-and-forget, at-least-once); `mesh agents`/`trust`.
- **`mesh setup`** — checks token resolution + gateway reachability, and
  points at `pip install synapse-gateway` when you want to host one.

**Temporal — durable execution, out of the box**
- The loop's recurring work is now DURABLE ACTIVITIES: plain JSON-able
  functions (golden_run, gaps_mine, shadow_evaluate, snapshot_pin,
  drift_audit) — unit-testable with NO Temporal server.
- `neuralosd temporal --setup` checks the Temporal CLI, a reachable server,
  and the temporalio SDK — each failure names its fix (`brew install
  temporal` / `temporal server start-dev` / `pip install temporalio`).
- `neuralosd temporal worker --instance-dir . --task-queue neuralos` runs a
  durable worker; `temporal schedule-create --cron "0 2 * * *"` builds the
  exact CLI call for a recurring loop schedule.
- `neuralosd temporal --run-activity golden_run --instance-dir .` runs any
  activity directly — no server needed.

Tests: 930 passed (22 new), smoke 48 (temporal activity block is
server-free).

## 2.4.1 — `neuralosd browser --setup`

- **Consent-gated agent-browser install** — `neuralosd browser --setup`
  checks for Node/npm, installs the agent-browser CLI via
  `npm i -g agent-browser` (prompting first; `--yes` for non-interactive
  runs), and verifies with a **live snapshot ping** (open example.com +
  snapshot + close). Already installed → never re-installs, just pings.
- **Honest failure modes, each pinned by a test**: no Node = error naming
  the prerequisite (npm never invoked); prompt EOF on a non-interactive
  stdin = a safe decline; npm "succeeds" but the binary is still off-PATH =
  reported with restart-your-shell advice, never a false green; installed
  but the ping fails (offline/sandboxed) = reported, exit 1.
- Everything injectable (which/installer/runner/prompt) — the full setup
  flow is unit-testable without Node, npm, or a browser. 8 new tests;
  smoke grew a live-ping block that runs wherever agent-browser exists.

## 2.4.0 — sources, proof, trust

**Sources**
- **agent-browser connector (tier 1 + 2)** — `init --source browser:<url>`
  profiles pages through the agent-browser CLI (accessibility snapshots →
  rows, roles → caged enums, refs → action handles); `neuralosd browser
  --session <name> <op>` drives persistent sessions (open / snapshot /
  click / fill / type / network / text). Missing CLI = error that names the
  fix; capture injectable; e2e-tested against the real CLI.
- **Authenticated API sources** — `init --source api:<url> --header H
  --secret-ref env:VAR|scrt:<store>:<key>|op:<vault/item/field>`; the
  instance stores only the reference, resolved at ask time via scrt /
  1Password CLIs. The secret never touches disk.
- **Multi-source instances** — `init --extra-source prices.csv --on
  region=region`: per-source caged counts (`count_in`) and a deterministic
  nested-loop `joined_count`.
- **Streaming windows** — log instances gain a caged `recent_events(minutes)`
  probe; unparsable timestamps are out of the window (an unknown when is not
  a recent when).

**Verification depth**
- **Value-level oracles** — golden cases carry `oracle.value + shape`
  (count / percent / ratio / date / text); `golden_run` verifies the ANSWER
  with typed invariants and reports `wrong_values` separately. Routing
  right + value wrong = FAIL.
- **Snapshot pinning** — `neuralosd snapshot --label q3` (hashed
  observations) and `ask --as-of q3` answer from the pin; the envelope
  carries label + hash — reproducible evidence.

**Trust & fleet ops**
- **RBAC / row-level caging** — `acl.json` roles with probe allow-lists and
  row filters; `load_instance(role=...)` and `ask --role` enforce both.
- **Refusal-rate SLO** — `neuralosd slo` computes the rate over a window
  and exits 1 above SLO.
- **Fleet dashboard** — `neuralosd fleet <dirs|globs>` reports per-instance
  health; exit 1 on any breach.
- **Golden-bank CI badge** — `neuralosd badge` emits shields.io endpoint
  JSON + a GitHub Actions workflow gating pushes on the bank.

**Reasoning quality**
- **Cluster-driven synonyms** — `neuralosd synonyms` proposes triggers from
  gap clusters (ownership evidence: audit score vectors, caged enums, name/
  description tokens); approved triggers extend derived metrics; probes.py
  probes get exact edit suggestions — generated code is never silently
  rewritten.

Tests: 899 passed (43 new), smoke 46.

## 2.3.0 — the loop grows up: ten closing moves

1. **Probe birth traps** — every reason-installed probe synthesizes its
   misuse trap into traps.json and PROVES the refusal with the live router
   before the loop reports success. A capability ships guarded, or the
   install fails loudly.
2. **Refusal clustering** — `gaps` clusters refusals into patterns with cost
   framing ("this gap cost you 412 refused queries across 38 phrasings").
   Prioritize by what users repeatedly want.
3. **Negative-feedback ingestion** — `neuralosd feedback` turns a
   thumbs-down into a bank candidate with the user's correction attached;
   `--apply` promotes only PROVEN candidates (refusal proven for traps,
   live routing for golden — the oracle stays flagged unverified).
4. **Schema-similarity probe transfer** — `neuralosd transfer` proposes a
   schema sibling's probes with the golden bank translated and re-oracle
   flagged; shared probes are reconfirmed. Proposals never auto-install.
5. **Oracle provenance** — `golden --record` stores who verified a value,
   when, from what source, how; `golden --receipts` prints the evidence
   chain. The golden bank is an evidence chain now, not just a test file.
6. **Shadow mode** — `reason --shadow` installs probes that cannot serve
   traffic; `shadow --evaluate/--promote` promotes only on oracle agreement
   across the shadow window. Kills the "generated probe confidently wrong"
   class.
7. **Consensus in the reason loop** — `--consensus-model` requires a SECOND,
   different model to independently pick the same ids; disagreement refuses
   the install.
8. **Auto-deprecation** — `neuralosd mothball` demotes probes nobody queried
   in N days (kept on disk, reinstatable); the menu filter never empties the
   instance.
9. **Schema-drift re-audit** — `neuralosd drift` re-profiles a changed
   source and flags the probes it breaks (name-caged columns, enum cages)
   BEFORE they fail at runtime; exits 1 when a re-audit is due.
10. **Federated gap learning** — `gaps --export-fingerprints` shares
    anonymized token-pair hashes + counts (never questions);
    `--ingest-fingerprints` merges peers' counts into `mesh_count`. The mesh
    crowd-sources capability discovery, privacy-preserving.

Also: smoke suite +4 loop checks (45 total); tests 856 passed.

## 2.2.4 — syslog path resolution (Windows binaries)

- **syslog source path resolution** — init now embeds `os.path.abspath` of
  the syslog source (relative sources resolve against init's own cwd); the
  smoke suite passes a relative source, so it is OS-neutral. Fixes the
  v2.2.3 Windows smoke failure (an MSYS `$PWD` path the native binary
  cannot open). Windows unit tests now pass, including the wevtutil
  end-to-end with retry.

## 2.2.3 — wevtutil e2e retry

- The runner's System channel is intermittently slow to expose events;
  the wevtutil e2e retries before declaring a zero-event capture.

## 2.2.2 — wevtutil parse hardening (Windows binaries release)

- **parse_wevt_xml hardening** — normalizes the remaining redirected-output
  quirks: BOM-less UTF-16LE (NUL heuristic in the decode layer),
  single-quoted XML namespaces, mid-document XML declarations. With this,
  the Windows wevtutil end-to-end test passes on the Windows runner and the
  full 5-platform binary set builds.
- e2e test now carries raw-capture diagnostics in its failure message.

## 2.2.1 — Windows event-log hotfix

- **wevtutil UTF-16 decode** — wevtutil writes UTF-16LE when its output is
  captured, so the Windows binary's event-log connector parsed ZERO events
  from a healthy capture. Tool output is now decoded BOM-aware (UTF-16 on
  BOM, UTF-8 otherwise), pinned by a unit test.

## 2.2.0 — deep Excel + log source connectors

- **Deep Excel connector** — `init --source book.xlsx` now profiles formulas
  (count + samples per sheet), all sheets (row/column/formula counts via the
  new `sheets` probe), and **named ranges** (profiled, resolvable to a
  bounded value preview via the `named_ranges` probe). New caged
  `formula_scan` probe ("formulas in <sheet>"). Header rows are no longer
  counted as data — xlsx now matches the CSV path.
- **Log source connectors** — three new `init` sources, all normalized to
  `{ts, host, app, level, message}` events so the standard probes work
  unchanged: `syslog:<path>` (RFC3164/RFC5424, stdlib only, parse errors
  recorded in the bridge's `LOG_INTEGRITY` — never silently dropped),
  `journal:[UNIT]` (journald via `journalctl -o json`), and
  `wevtutil:<Channel>` (Windows Event Log via `/f:XML`). All reads bounded
  by `NEURALOSD_LOG_CAP` (default 10000), newest first; the capture layer is
  injectable so journald/wevtutil are unit-testable off-host.
- **Menu-instance module leak fix** — `menu_adapter._load_module` popped the
  sibling module only on FAILURE; on success `bridge` stayed registered and
  the next instance loaded in the process silently reused this instance's
  data layer. Now popped on both paths (completes the v2.1.0 fix that
  covered the probes.py path); test suites that import a generated
  `bridge.py` directly now clean up after themselves too.
- **Docs** — sources table covers the three log prefixes and the deep Excel
  profile; two new cookbook recipes (workbook audit, log instances);
  smoke suite grew the xlsx-deep + syslog blocks (41 checks).

## 2.1.0 — SQLite connector + auto-trap synthesis

- **SQLite source connector** — `neuralosd init --source app.db` profiles a
  SQLite database with stdlib `sqlite3` (zero new deps, works in the frozen
  binary): every table profiled (columns, types, row counts), all tables
  named in the generated `probes.py`, first table drives the column probes,
  and the scaffolded instance answers `"how many rows"` immediately.
- **Auto-trap synthesis** — every registered probe gets its misuse trap in
  `traps.json` at birth: `"<first trigger> <probe name>"` with
  `expect_refusal: true`. The probe's raw name (e.g. `row_count`) is a token
  no trigger knows, so a conforming router refuses the trap and a router
  that drifts answers it — the refusal contract is regression-tested from
  the moment a capability exists. Idempotent, deduped across loads, gated
  by `NEURALOSD_AUTO_TRAPS=0`. Golden items are never auto-added — they
  need human verification.
- **Cross-instance bridge fix** — loading a second instance in one process
  no longer reuses the first instance's `bridge.py`; each instance's
  sibling modules are dropped from `sys.modules` after load, so every
  instance answers from its own data file.
- **Docs** — SQLite source documented in usage §3.0 and a new cookbook
  recipe; smoke suite grew an end-to-end `.db` init check (31 checks).

## 1.10.0 — multi-instance & mesh era

- **Multi-instance serving**: one process, many instances, routed by path
- **Parallel probe sweep**: the reason loop's probe scan is now concurrent
- **Mapper verdict cache**: repeat reason runs skip the model entirely
- **Template fork**: deploy from a baked sandbox snapshot in seconds
- **Synapse mesh integration**: register, serve, discover, delegate
- **REST-preferred boxlite door**: no runtime lock when boxlite serve is available
- **Plural-normalized scoring**: "clinics" matches "clinic" everywhere
- **Exact-phrase bonus**: a fully-covered trigger beats a partial match
- **Shared action-intent check**: the gate and the floor-miss path use the same code
- **Whole-word qualifier matching**: "resolved" doesn't fire inside "unresolved"

## 2.0.6 — the SDK release

- **`neuralosd serve --auth-token`** (or `$NEURALOSD_API_TOKEN`): bearer/X-API-Token
  auth on every endpoint except liveness (`/healthz`, `/ready`); constant-time
  comparison; explicit non-loopback warning tells you to use it.
- **`neuralosd_sdk`** — zero-dependency Python client: typed `AskResult`,
  exponential-backoff retries on connection errors and 5xx (4xx never
  retried), `AuthError` / `RefusalError` / `TransportError`.
- **JavaScript client** — `clients/javascript/neuralosd-client.mjs`, same
  contract for browsers and Node 18+, zero dependencies.
- **CI test gate** — unit tests + the CLI smoke suite run on every PR.

## 2.0.5 — sidecar delegation, complete

- Classic **menu-only instances** (no `probes.py`) are fully supported by the
  sidecar: `needle_menu.json` is lifted onto `bridge.py` via the shared
  `menu_backed_probes()` path.
- **Lazy imports in helper functions** now delegate: any
  `ModuleNotFoundError` escaping a menu-lifted probe retries the whole probe
  in the sidecar (the menu path previously skipped the fallback wrapper).
- **`"tier": "sidecar"` honored from menu entries** — the classic-layout
  escape hatch (the adapter hardcoded `in-process`).

## 2.0.4 — instance import audit

- **`neuralosd audit`** — AST-parses every instance module and classifies
  imports as `bundled` / `host-only` / `instance-local` /
  `missing-everywhere` against the runtime and the sidecar (empirical
  `find_spec`, never assumed). `--root` scans a fleet; `--json` for CI;
  non-zero exit on any missing-everywhere.
- `neuralosd init` runs the audit on every scaffold and warns before an
  under-provisioned instance ships.

## 2.0.3 — component refresh

Sandbox/model component baseline refresh (auto-update).

## 2.0.2 — frozen-binary import fix

- **sqlite3 force-included** in every frozen variant — menu bridges doing
  `import sqlite3` load on every platform (previously a Windows DLL-load
  failure surfaced as a misleading "declares no probe that a callable
  actually backs").
- **CLI surfaces real import failures** — the per-module error is appended
  to the hint, so users see `ModuleNotFoundError: _sqlite3` instead of
  debugging the wrong thing.
- Release-pipeline repairs: tag pushes from automation now trigger builds
  (PAT checkout), manual PyPI publishes work (twine + token), and the
  build's PyPI gate accepts backfills of older versions.

## 2.0.0 / 2.0.1 — production hardening

Deterministic-first runtime baseline: confidence gates, refusal semantics,
caged arguments, audit ledger, PII masking, sidecar, derived metrics,
reasoning loop, multi-instance serving.
