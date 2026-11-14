# Changelog

## 3.0.2 — security review

A security pass over the agentic surface and the deploy path.

- **Identity is bound to a credential.** Per-agent tokens (`acl.json`
  `agents.<name>.token_sha256`) replace self-asserted `agent_id` over HTTP:
  a conflicting claim is `403 agent_mismatch`, the role comes from the ACL
  entry, and an unlisted agent is denied.
- **Cross-agent access closed.** An agent sees only its own `/audit` rows
  and `/runs/{id}`; the operator token sees all.
- **Wide binding fails closed.** `serve()` defaults to `127.0.0.1` and
  refuses a non-loopback bind without a credential; the multi-instance
  server refuses too.
- **Deploy command injection fixed.** The msb start line is shell-quoted and
  the boxlite start file uses a list-based `Popen`; the inner service binds
  loopback instead of `0.0.0.0`.
- **Filename traversal closed** for snapshot labels and schedule names.
- **Resource bounds:** 64 concurrent HTTP workers and a 30 s socket timeout.
- **Role escalation closed** for agent tokens.

See `docs/security.md`; findings I1–I9; tests
`tests/test_security_hardening.py`.

## 3.0.1 — production hardening

A hostile-input pass over the agentic surface, aimed at a commercial
deployment. Found and fixed:

- **Session ids are sanitised** (`safe_name`): a caller-supplied id is
  reduced to a safe filename and hash-suffixed, so it cannot traverse out of
  `.agentic/memory` or collide with another id. Session files are now
  bounded (`max_records`).
- **HTTP bodies are bounded** at 1 MB (413), with a bounded drain so the
  client can read the error; `/audit` streams with a safe limit and answers
  a junk `limit` instead of resetting the connection.
- **ACL budgets are parsed defensively**; a non-numeric value no longer
  raises on every request.
- **Mutating tools require admin** once an `agents` block exists
  (`build`/`shadow`/`mothball`/`delegate`); an instance with no `agents`
  block keeps the trusted-operator default.
- **HITL refuses by name** if the probe disappeared between the token and
  the confirmation, instead of raising `KeyError`.
- **Bounds**: trace counters and per-agent budget maps are capped and
  pruned; the audit query keeps only the newest matches in memory.

Findings H1–H8 are in `docs/audits/findings.md`; the executable contracts
are in `tests/test_agentic_hardening.py`.

## 3.0.0 — every state machine explicit

The agentic surface added mutable state the F-series census did not cover.
This release extends the census (sections 13–19) and makes the new machines
explicit in code, with transition tables and tests:

- **Async runs (`RunStore`)**: `queued → running → done | failed |
  cancelled`, terminal states final. Fixed a cancellation observed during a
  probe being overwritten by the late success, and terminal runs being
  movable again.
- **HITL tokens (`ConfirmStore`)**: `pending → confirmed | expired`,
  single-use, and the three failure answers are now distinct — `invalid`,
  `expired`, `already_used` — instead of one "invalid or expired" string.
- **Scheduler**: fixed a deadlock in `remove()` (a non-reentrant lock plus
  `_save()` taking it again); `schedules.json` and each result file are now
  written atomically with a closed handle; each entry records
  `last_run`/`last_status`/`last_error` instead of only a counter.
- **Traces** are bounded per run (`max_events`) and rewritten atomically
  once the bound is reached.
- The door registry, sidecar provisioning and the MCP instance caches are
  documented in the census; the registry already saved atomically.

Findings G1–G6 are recorded in `docs/audits/findings.md`; the executable
contracts are `tests/test_agentic_state_machines.py` and
`tests/test_scheduler_state.py`.

## 2.9.16 — the agentic surface

An agent harness does not ask once; it plans, retries, delegates and runs
long. The MCP server was a dead end for that caller: it required
`instance.py` + `needle_menu.json` (the scaffolder writes `probes.py` +
`bridge.py`, so `tools/list` crashed), and `tools/call` executed
`fn(**args)`, bypassing the router — no grammar cage, audit row, PII mask,
row cap, results gate, refusal or cache.

**The MCP surface is now the guarded router.** Fourteen tools: `ask`,
`search`, `describe`, `catalog`, `explain`, `health`, `memory`, `trace`,
`delegate`, `discover`, `build`, `shadow`, `mothball`, `confirm`. A refusal
is a result with a machine-readable `refusal` object (`code`, `retryable`,
`suggested_action`, `candidates`, `missing`); answers carry
`content_trust: untrusted` and `provenance`; `describe` exposes a
`tool_version` so a menu change is visible.

**Agent contracts.** `agent_id` / `session_id` / `run_id` are stamped on
the audit row. `acl.json` gains an `agents` allow-list with a per-agent
`role`, `pii` policy and call `budget`. `idempotency_key` replays the first
result instead of executing again. Sessions remember and recall; runs are
traced per episode; probes flagged `confirm=True` pause for a HITL token
instead of firing; mesh delegation (`delegate`/`discover`) and the verified
`build`/`shadow`/`mothball` lifecycle are tools.

**The HTTP service carries the same contracts.** `POST /ask` (sync or
`async: true`), `GET /runs/{id}`, `DELETE /runs/{id}`, `GET /audit`,
`GET /tools`, `POST /v1/confirm/{token}`; `X-Agent-Id` / `X-Session-Id` /
`X-Run-Id`, `Idempotency-Key`, per-agent policy and budget (429 +
`Retry-After`), and the refusal contract on 422. `neuralosd ask --explain`
is a dry run that executes and audits nothing.

## 2.9.13 — a refusal rate needs a sample

**SLO — one refusal was a 100 % rate and a fleet incident.** `refusal_rate()`
breached whenever `asks > 0` and the rate was above the SLO, so a single
correctly-refused ask in a 24 h window paged a scheduler. On a 21-instance
fleet three low-traffic instances flapped to BREACH every window, and
`fleet` exited 1 on the artefact.

A breach now needs a sample. Below `MIN_ASKS_FOR_SLO` (10, `--min-asks N` or
`NEURALOSD_MIN_ASKS`) the window reports `insufficient_data: true` and
`breach: false`; exactly 10 asks is judged normally, and a
`max_refusal_rate` of 1.0 or more can never breach. The rate itself is never
hidden — undersized windows still report `refusal_rate`, `asks` and
`refused`, so thin traffic stays visible. `fleet_report()` carries the new
fields, counts undersized instances in their own additive `undersized`
bucket, and both `slo` and `fleet` render them `n/a` (distinct from `ok`)
and exit 0; only a real breach drives the exit code. The default 40 % SLO,
the window/source/tail semantics and the `ask_audit.jsonl` row contract are
unchanged.

## 2.9.12 — the audit release: fourteen defects, every state machine named

A full audit of the implicit state machines behind the runtime. Every defect
below was reproduced before it was fixed; every fix carries a regression
test, and the census lives in `docs/audits/state-machines.md` with the
reproductions in `docs/audits/findings.md`.

**Actuation — the one-shot guard could be permanently disabled.** The guard
asked whether a plan had EVER been undone, so one early rollback let a later
replay fire the action twice. State is now the plan's LAST transition
(`(none) → DRY_RUN → APPLIED → UNDONE → APPLIED`), a second undo is refused
instead of firing the reverse action again, and apply/undo refuse a
substituted action or target under a plan id (the plan is a promise about
what will be touched). A torn trailing journal line — the normal result of a
crash mid-append — is tolerated and repaired at the writer; corruption with
valid rows after it refuses by name rather than deriving state from a
partial history. Pre-v2.9.10 rows without `op` are read through their flags,
so an upgraded process still refuses a duplicate.

**CLI — `--plan` was a trigger, not a selector (P0).** `neuralosd actuate
--plan <id>` without `--apply` fired the side effect; the rollback spelling
the apply notice itself printed (`--undo --plan`) was routed to APPLY; and a
bare `--undo` silently dry-ran. Firing now requires the explicit `--apply`
word, `--apply` and `--undo` are mutually exclusive, and both require a
plan.

**Service — a role header reset the socket (P0).** The HTTP boundary tested
an undefined `_acl_roles` name, so any request carrying `X-NeuralOS-Role` to
an instance without `acl.json` raised `NameError` and the client saw a
closed connection instead of the documented 403. One `acl_roles()` reader
now serves the resolver and the boundary, and a torn or wrong-shaped
`acl.json` reads as "no roles". `/ready` is documented as authenticated (it
executes a probe); only `/healthz` is open.

**Multi-instance door — malformed questions were answered.** `{"question":
42}` answered "42" and `{"question": ""}` performed a real ask. The door now
mirrors the single-instance contract (malformed JSON, non-object body,
non-string question, empty question → 400) and dispatches on a normalized
path, so `/healthz?lb=1` and `/alpha/ask?trace=1` reach their routes.

**Router — the refusal vocabulary was incomplete, and a corrupt instance
config changed the operating point.** `below_floor`, `ambiguous` and
`comparative` were returned by the gate and written to the audit ledger but
missing from `REFUSAL_REASONS`; they are now declared (`GATE_VERSION` stays
1 — no semantics changed), and an AST test fails if `_gate_decide` ever
returns a literal outside the set. An instance-scoped `router.json` that
exists but cannot be parsed now refuses by name and path instead of silently
falling back to the cwd's thresholds.

**Lifecycle, watch, and hygiene.** `mothballed.json` is written atomically
and read tolerantly (a torn marker no longer reads as "nothing demoted" or
crashes fleet/SLO). A rule's per-rule `webhook` is carried into the alert it
produces, so it is actually delivered. The unreachable `_cmd/gossip.py` shim
is removed, and every `_cmd` shim's argparse-dest contract is now enforced
by a sweep. File handles are closed in `Instance.export`, the bank readers,
and five command modules.

Full suite: 1341 passed, 8 skipped. Bundled itsm golden 13/13, truth 13/13,
invariants hold. Wheel-vs-source differential parity on the gate decision.

## 2.9.11 — one gate, one directory: the calibrated floor belongs to the instance

Three defects found while verifying v2.9.10, reproduced, fixed and
regression-pinned. Two are in the actuation CLI, one is the routing gate
that was reported as "the binary and the wheel disagree".

**Actuate — the CLI could never actually apply.** `neuralosd actuate
--apply --plan <id>` dry-ran forever: the CLI shim read attributes the
argparse parser never defines (`apply_plan`/`undo_plan_id` where the
dests are `plan`/`undo_plan`), so apply and undo were both unreachable
and the blind-apply guard could not fire. The shim now falls back across
the real dest names and `--apply` without a plan refuses with exit 2 and
a message that names `--plan`. Pinned in `tests/test_actuate_cli.py`
(subprocess CLI, argparse-dest contract test that fails if any
`getattr` chain in the shim stops resolving).

**Actuate — the stub executor printed "✓ APPLIED".** The CLI wires
`side_effect = lambda: None`, so a "successful" apply changed nothing on
the host while the journal recorded `applied: true`. The journal may
never claim an effect that did not occur: apply/undo notices now print
an explicit STUB warning and journal entries carry `"stub": true` (and
undo reports echo it). A future real executor flips one flag.

**Routing — `router.json` was read from the process CWD, so the
calibrated floor was a coin flip.** `calibrate` writes the fitted
floor/margin into the INSTANCE directory, but the router looked for
`router.json` in the CURRENT WORKING DIRECTORY only. The same instance
answered differently depending on which directory the CLI was launched
from: launched inside the instance dir, "list the distinct tiers"
against a floor-30 calibration refused `below_floor`; launched from the
parent, the floor silently fell back to the default 3.0 and the same
question was answered. Reproduced with a SINGLE v2.9.10 binary from two
directories — this is the v2.9.10 "wheel vs binary gate divergence":
same source, different launch directory. Thresholds are now resolved
from the instance directory first (cwd stays as a legacy fallback), and
a new CI job (`verify-release.yml` → `differential`) asks the same
calibrated question of the published binary and the wheel from a parent
directory and asserts byte-identical gate decisions on every release.

**Routing — the entity bypass was switched off by a coincidence word.**
The gate's bypass (`any extracted value appears in the question →
skip floor, margin and qualifier checks`) counted values that
`extract_args` had filled from an OPTIONAL argument's default — an enum
fallback like "all", a default page number, `status="open"`. A sentence
that merely contained that word turned a weak sub-floor match into a
confident answer. Worse, enum extraction is substring-based, so a
default value appearing as a word is indistinguishable from one the
question deliberately named. Bypass now requires a NON-DEFAULT argument
(no `required: False`, no `default`) whose value the question contains;
required args only ever hold genuinely extracted values. Optional-arg
questions must now be carried by their prose score — conservative by
design. The scoring term (`ARG_MATCH_BONUS`) keeps its original
behaviour.

**Publishing the gate decision path.** `NEURALOSD_GATE_TRACE=1` prints
one JSON line to stderr — probe, score, floor, margin, per-probe gate,
args_seen, extracted args, decision — for every gate evaluation,
identically in the wheel and a frozen binary; `Router.last_gate_trace`
keeps the last decision for tests. A build-vs-build divergence is now a
two-line diff, and the differential CI job asserts the decision path is
published by both builds.

Pinned in `tests/test_gate_trace_and_config.py` (11 tests: instance-dir
resolution, cwd legacy fallback, default-word bypass, named-optional
non-bypass, required-enum bypass, trace contents, stderr publication,
trace-off by default). Full suite: 1254 passed, 8 skipped. Bundled itsm
golden 13/13, truth 13/13, invariants hold.

## 2.9.10 — one-shot actuation, intent-faithful routing, a bounded ask cache

Three defects reproduced in the wild, fixed and regression-pinned.

**Safety — actuation is one-shot.** Re-running `--apply --plan <id>`
fired the side effect a second time (duplicate firewall rule, a host
isolation applied twice — whose second application could defeat the
first undo). The APPLY branch only ever checked that a dry-run with the
plan id existed; nothing refused an applied → applied transition. State
is now read from the journal — durable, because the CLI is a new process
every time — and `dry_run → applied → undone → applied` is the only
legal path. A replayed `--apply` is refused by name and pointed at the
undo path. There is deliberately no `--force`.

**Routing — the breadth term measured repetition, not coverage.**
`score_probe`'s breadth bonus was `1.0 * sum(overlaps)`, which grows with
the NUMBER of triggers: a probe with 22 variants of "<qualifier>
detections" outranked (32.3 vs 22.3) the probe whose single trigger was
the question verbatim — "are detections increasing" routed to the
detections listing, and a dispositions probe ranked below the detections
probe, so the discard gate refused a question the instance could answer.
Breadth now counts the DISTINCT question tokens any trigger covers —
bounded by the question length, therefore not gameable by adding
synonyms. Best-term, exact-phrase bonus, name/description terms and the
unconsumed-argument penalty are untouched; the bundled itsm golden bank
re-runs byte-identical. `neuralosd lint` now reports `trigger_spam`:
probes holding ≥ 3 trigger variants that share ≥ 80 % of their tokens
(advisory — legitimate paraphrase clusters trip it too; collisions
remain the failing condition).

**Cache — dead entries accumulated forever.** `_cache_load()` correctly
refused entries with a wrong `menu_version`, past the TTL, or malformed
payloads — but `_cache_store()` only ever appended, so refused rows
accumulated for the life of the instance (measured: 13 live keys inside
a 2.5 MB `.ask_cache.json`, every ask re-serialising the whole file).
The store now evicts dead states on every write and caps the cache at
`CACHE_MAX = 500`, keeping the newest. No migration, no rewrite of
legacy rows, no fabricated timestamps: existing files keep loading and
are pruned on the next write.

## 2.9.8 — durable open errors name their cause

Found seconds after shipping 2.9.7, driving `--durable` against a
models.json custom provider on a real host: the bridge's open op returns
a precise error (`provider superagent unavailable: no built-in module`) —
but the driver discarded the open response, so the first submit raised
the misleading `pi durable submit: not open` instead. The cause was
visible only in the bridge's stderr.

`DurableBuilder.open()` now raises `SystemExit` with the bridge's message,
the same contract submit already had — and the usage doc's claim that
"the bridge says `provider <name> unavailable`" is finally true at the
user's terminal.

## 2.9.7 — durable bridge: it ships, and it launches anywhere

Two defects in the `--durable` path, found by wiring pi builds on a real
host:

**1. `pi_durable_bridge.mjs` never shipped.** The file lived in the source
tree, so every dev checkout worked — but `[tool.setuptools.package-data]`
listed docs, skills and templates and NOT `*.mjs`. Every pip install of
2.9.6 (and earlier) contained a `pidurable.py` whose bridge script did not
exist: the node child died instantly with module-not-found and the driver
surfaced it as a bare `BrokenPipeError` on the first submit. The
pi_durable doctor check could not catch it because it only verifies that
node can import the npm packages, not that OUR bridge exists. Binary
releases were unaffected (Nuitka bundles the source tree directly).

**2. Even when shipped, the bridge could not load.** A bridge file's ESM
imports resolve from the FILE's own directory walking up — never from the
cwd. A pip-installed bridge (site-packages) can therefore never import
`@earendil-works/*`: the packages live in some unrelated node_modules the
bridge's ancestry never reaches. Dev checkouts only worked by accident
(the repo's own node_modules sat above the source tree).

- `*.mjs` is now in package-data; the wheel carries the bridge.
- `pi_durable_command` walks up from the instance dir (exactly like node)
  to find the node_modules holding `@earendil-works/pi-durable`, stages
  the packaged bridge into `<dir-of-node_modules>/.neuralosd-bridge/`,
  and launches it from there — every `@earendil-works/*` import resolves.
  The stage is content-compared against the packaged copy on every build
  (self-heals upgrades and hand-edited corruption); without any
  node_modules it falls back to the packaged path.
- `pi_durable_available()` (doctor) now also verifies the packaged bridge
  file exists, so defect class 1 fails at doctor time, not at a user's
  `--durable` invocation.
- New `tests/test_packaging_completeness.py`: every non-.py file under
  `src/neuralosd/` must be matched by package-data patterns (any future
  loose file fails CI at test time), plus staging/fallback/staleness
  tests for the launcher.
- Documented the provider split: one-shot builds accept every
  models.json custom provider (pi CLI resolves them); durable builds
  accept pi-ai built-in providers only.

## 2.9.6 — audit: every refusal dated + self-test traffic out of the SLO

Two defects were corrupting the fleet refusal-rate SLO — the number the
whole fleet reads as "how often does neuralOSd honestly decline":

**1. One refusal branch emitted undated, mode-less rows.** The
`model_fallback is None` branch (empty retrieval or unextractable args with
no model to ask) built its NoResults envelope by hand and skipped the two
keys every other refusal carries: `ts` and `mode`. A `neuralosd slo
--window 1h` read therefore counted the refusal (the row exists) but could
not DATE it — the windowed denominator silently dropped those refusals, so
a fleet refusing hardest exactly when the model was absent looked
suspiciously healthy. The branch now routes through the same `_refuse()`
path as every other refusal: full envelope (`ts`, `mode: "refused"`,
`refusal_reason`, `probe: null`, `results: null`), and the uniform prose
`refused (reason): the menu cannot answer this confidently, and guessing
is worse` replaces the old one-off sentence. The bare token in
`refusal_reason` is unchanged (`no_probe_matches`, `args_not_extractable`,
...) — clients branching on the token see no difference.

**2. Self-test traffic was counted as user refusals.** Every `neuralosd
golden` / `truth` bank replay, every periodic `/ready` watchdog tick and
every trap-synthesis verification appended to the SAME `ask_audit.jsonl` a
fleet's SLO reads — so a CI that replays its 40-question bank every 5
minutes multiplied the denominator with machine traffic while the
"refused" numerators climbed on trap rows. Every audit row now carries a
`source` stamp — `user` | `watchdog` | `golden` | `trap` | `serve` —
applied once at the `_audit` choke point (on a copy: cached envelopes are
re-audited on cache hits and must not leak a stamp). Bank replays write to
a SEPARATE ledger: `ask_audit.selftest.jsonl`, overridable per run with
`neuralosd golden --audit FILE` / `neuralosd truth --audit FILE`, or per
environment with `NEURALOSD_AUDIT_FILE`. The user ledger is restored on
even a crashed run. `neuralosd slo` (and the `refusal_rate()` reader)
gains an opt-in `sources=("user",)` filter and reports `rows_missing_ts`
for pre-2.9.6 rows instead of inventing dates for them: **no in-place
migration, no fabricated timestamps, no default denominator change** —
dashboards only move if you opt in to the filter.

Also: the mesh door (`neuralosd mesh ask`, the Synapse `SERVE` handler)
and HTTP `/ask` stamp `serve`; `/ready` stamps `watchdog`; the strict
discard path now emits the complete refusal schema with its long-registered
but never-emitted `dropped_filter` token; the SDK no longer truncates the
422 body to 300 chars before parsing, which silently turned any refusal
with a rich envelope into `reason=None`; and `refusal_body()` output is
byte-identical to the ledger row for the same ask.

Backwards compatible: additive keys only, refusal tokens unchanged,
`/ready` and multi-mode auth contracts untouched, existing ledgers are
read as-is (legacy rows count as `user`, as they always implicitly were).

## 2.9.5 — serve: multi-instance startup crash + auth enforcement

Two defects in `neuralosd serve --instances-dir` (multi-instance mode), both
shipped in 2.9.4 and every 2.9.x, both first hit in the published Windows
binary:

**1. Startup crash (all platforms).** `serve --instances-dir DIR` died before
binding a socket with `NameError: name 'os' is not defined` — `run_multi()`
uses `os` four times but only `run()` (the single-instance path) had an `os`
binding, via a function-local import. The CLI layer was the one layer
tests never exercised: `tests/test_multi_instance.py` built
`MultiInstanceServer` and `make_handler` directly, skipping the code that
ships. Fixed by hoisting `os` to a module-level import; an AST sweep of the
whole package for the same defect class (a name whose only binding is a
sibling function's local import) found no other instances — the remaining
candidates are all closures over an enclosing function's import. Also added
`src/neuralosd/__main__.py` so `python -m neuralosd` works like the console
script (it previously died with "cannot be directly executed", which is why
the subprocess smoke test needs it).

**2. Auth bypass.** `--auth-token` / `$NEURALOSD_API_TOKEN` was resolved and
enforced only by the single-instance path. Multi mode never read the token:
`serve_multi()` had no auth parameter and `multi.py` had no Authorization
check and no 401 anywhere — so binding wide with a token served every
instance's data unauthenticated, with none of the loud warning the
single-instance path prints. Multi now matches the single-instance contract:
with a token configured, every route except `/healthz`, `/{name}/healthz`
and `/{name}/ready` requires `Authorization: Bearer <token>` or
`X-API-Token: <token>` (constant-time compared, same 401 body); startup
prints `[token auth ON]` / `[NO AUTH]`; a non-loopback bind without a token
prints the same loud warning (existing policy is warn-then-bind — the
silence was the dangerous part). Routing, response shapes and lazy loading
are unchanged; no new dependencies.

Test discipline: the new CLI-layer tests call `run_multi()` with a real
argparse namespace and two minimal instances, one test shells out to
`python -m neuralosd serve --instances-dir ...` (a crash before bind can only
be caught in a subprocess), and the auth tests pin the full 401 matrix.
Every new test fails on 2.9.4 and passes after.

**Release verification for the artefacts themselves:** new
`verify-release.yml` workflow (manual dispatch, takes a tag) downloads the
published binaries from the GitHub release and — on a real windows-latest
runner, where 2.9.4 crashed — re-runs the CLI smoke suite plus a
multi-instance regression: clean missing-dir failure (no NameError), server
up, `/healthz` open, `/instances` and `/{name}/ask` 401 without credentials,
Bearer and X-API-Token reach both instances.

## 2.9.4 — mesh hardening: preflight, provenance, trust, verification

> Numbering note: 2.9.3 was consumed by the component auto-updater between
> here and the mesh work below (boxlite 0.11.0 → 0.12.0 — a dependency-only
> release with no code changes). PyPI never reuses a version, so the mesh
> release ships as 2.9.4. The auto-updater now bumps ALL FOUR version
> carriers (pyproject, runtime `__init__`, SDK `__init__`, npm package.json)
> — 2.9.3 left the SDK and npm behind on 2.9.2, which broke the
> versions-equal-across-the-board invariant that
> `tests/test_component_versions.py` pins.

The Synapse-gateway integration grew from "a REST client and an ask loop" into
a protocol: every verdict carries its versions and a signature, callers are
classified before anything crosses the boundary, and a peer can be asked
*would you answer this* before anyone pays for a round trip.

**Preflight — `Router.dry_run()` / `MeshAgent.preflight()` / `mesh preflight`**
- `Router.dry_run(question)` is the decision half of `ask()` with the
  execution half removed: same normalisation, same action-intent pre-check,
  same retrieval, same argument extraction, same calibrated gate — but no
  probe runs, nothing is cached, nothing is audited. Returns `would`
  (`answered` / `refused` / `defer_to_model`), `would_answer`, `probe`,
  `refusal_reason`, the score vector and `gate_version`. Kept honest by a
  conformance test that asserts dry_run agrees with ask() on every fixture
  case, in both directions, with matching refusal reasons.
- `MeshAgent.preflight(q)` publishes that decision with the menu/gate/protocol
  versions and a signature block — the discovery ping a mesh caller makes
  before delegating.
- `neuralosd mesh preflight <instance> "question"` exits 0 when the instance
  would answer, 2 when it would refuse; `mesh ask --delegate` and
  `mesh delegate --preflight` use it to skip peers that would only refuse.

**Provenance — every verdict says what produced it**
- Answers and refusals now carry `menu_version`, `gate_version` and
  `mesh_protocol_version` (1). A peer that cached an answer can detect menu
  drift instead of reusing a stale one.
- `answer_hash()` is deterministic over the *sent* view (`ts`, `latency_ms`,
  `cached` and friends excluded); `sign_envelope()` signs that hash —
  hmac-sha256 when `NEURALOSD_MESH_KEY` is set, plain sha256 integrity-only
  otherwise — and `verify_signature()` returns `(ok, reason)` with bare
  reasons (`signature_ok`, `tampered`, `unsigned`, `integrity_only`,
  `no_key`, `bad_signature`, `unknown_alg`). Redaction happens BEFORE
  signing: the signature always matches what was sent.

**Trust classes — what crosses the boundary, applied uniformly**
- `TrustPolicy.for_level()` over `self` / `trusted` / `known` / `untrusted`
  decides four toggles: expose the score vector, expose candidates, expose
  the discarded-filter ledger, allow the caller to delegate through us.
- Redaction is labelled: a `known` caller sees a `confidence_band`
  (high ≥ 2×floor, medium ≥ floor, low below) instead of raw scores, and a
  withheld filter ledger is marked `"ledger": "withheld"` so the accounting
  looks withheld, not absent. The band is computed BEFORE redaction —
  redaction may hide how confident, never lie about it. `untrusted` callers
  get `"delegation_allowed": false`.
- Capabilities advertised to the mesh carry probe triggers and argument
  TYPES, never enum VALUE lists: a stranger who can read the values can read
  the rows.

**Protocol negotiation**
- `handle(mesh_protocol_version=2)` answers `incompatible` with bare
  `error_code: "mesh_protocol_mismatch"` — gate vocabulary (`refusal_reason`)
  is never faked for a protocol problem.

**Peer discovery with the router's own scorer**
- `discover_peers(transport, question, self_id)` scores published peer
  capabilities with the same tokeniser that routes local questions: probe
  triggers first, bare skill names as fallback, generic-intent words ignored.
  Malformed directory entries are skipped, self is excluded, results are
  capped. With `preflight=True` each shortlisted peer is asked would-you-
  answer; refusers are dropped, a peer without the verb is kept but marked
  unverified (`would: None`), other errors marked `unreachable`.

**Honest delegation — `delegate_to_peer()`**
- Refusals travel as refusals: a peer's `refused` verdict surfaces with its
  own `refusal_reason`, not as an error. An answer is verified against what
  the peer ADVERTISED (probe known, results non-empty, menu/gate/protocol
  current) and stamped `verified: true` / `answered_unverified`.
- Safety rails: visited-set loop detection, a hop budget (`MAX_HOPS=3`), a
  deadline checked before the call and mapped onto the socket timeout, and
  transport failures carrying bare error codes (`unreachable`, `timeout`,
  `rate_limited`, `gateway_error`).
- The legacy call shape is preserved: `result["response"]` stays the raw
  peer response, `result["peer"]` the peer id.

**Registration and heartbeats**
- `MeshAgent.register()` builds the versioned registration blob without
  touching the network; publishing is explicit (`transport.register(reg)` /
  `mesh register --publish`).
- `heartbeat()` detects menu drift against the previous beat;
  `heartbeat_loop()` is bounded (`max_beats`), annotates publish failures
  with the error code, and tolerates transports without the verb.

**Gossip — proposals, never auto-application**
- `gossip_blob()` exports gap fingerprints (sha256 pairs, questions never
  leave the edge) with the menu digest. `explain_fingerprints()` inverts
  them OWNER-SIDE only: given the fingerprints plus your own raw audit, you
  recover token clusters — a stranger holding only the fingerprints recovers
  nothing (`known: false, tokens: []`).
- `propose_triggers()` now carries an honest verdict per cluster:
  `covered` (already in the menu), `extend_trigger` (every core token known
  somewhere in the menu — a phrasing variant), `new_subject` (a token NO
  probe knows — a trigger would teach the router to answer a question the
  probe cannot; it needs a new probe) and `new_probe` (nothing scores).
  Proposals remain advisory.
- `cluster_gaps()` accepts bare question strings as well as audit dicts.

**Transport**
- Retries only what is provably idempotent: GETs, and POSTs carrying an
  idempotency key (explicit or derived from `taskId`) — which also travels
  on the wire as an `Idempotency-Key` header so a gateway-side retry
  collapses onto the same work. Everything else fails fast. Backoff is
  `min(2.0, 0.25·2^attempt)` over retryable codes only.
- HTTP errors map to bare codes (`401/403 → unauthorized`, `404 →
  not_found`, `409 → conflict`, `429 → rate_limited`, `502 → gateway_error`)
  and an unreadable error body no longer masks the mapping.
- Deadlines (`Deadline.from_ms`) bound the socket timeout from above
  (`max(0.1s, min(timeout, remaining))`); a spent deadline refuses before
  any call.

**CLI (`neuralosd mesh`)**
- New subcommands: `preflight`, `delegate`, `gaps`, `verify`, `register`,
  plus `discover --preflight --json`, `serve --publish --heartbeat`, and
  `ask --delegate --preflight --max-hops --deadline-ms --trust`. Exit codes
  are contracts: preflight 0/2 by would-answer, delegate 0 only when the
  answer verified, verify 0/1, ask 2 on refusal unless delegation produced a
  response.

**Tests** — 98 new: the dry_run conformance suite (agreement with ask() on
all 16 gate cases, side-effect freedom, out-of-range parity, envelope shape)
and the mesh protocol suite (preflight, provenance, trust matrix, signatures,
discovery, delegation, gossip, transport retry, deadlines).

## 2.9.2 — Windows console crash on `docs`

- `neuralosd docs usage` (the first command a new user runs) exited 1 with
  `UnicodeEncodeError: 'charmap' codec can't encode characters` on Windows,
  where the console defaults to cp1252 — and on any POSIX C locale, where it
  defaults to ASCII. The CLI now reconfigures stdout/stderr with
  `errors="replace"`, so an unrepresentable glyph degrades to a placeholder
  instead of taking the command down. Found by the new
  `.github/workflows/robustness.yml` windows-latest job, which is why that
  matrix exists.
- Regression test simulates a cp1252 stream, so the crash is caught on every
  platform, not only on Windows.

## 2.9.1 — robustness sweep (false positives, false negatives, edge cases)

An adversarial hunt across the router, the HTTP service, the SDK and the
source connectors. Every fix below started as a failing case in
`tests/test_edge_case_hardening.py` or `tests/test_service_refusal_contract.py`.

**Correctness / false answers**
- Duplicate probe names are now a hard error (`ValueError`) at `Router`
  construction — before, the first definition won silently while the menu,
  OpenAPI and chains disagreed about which one was live.
- Cyclic probe results no longer `RecursionError` the audit/masking pass;
  cycles render as `"<cycle>"`.
- `Decimal` / `date` / `datetime` / `bytes` in a result no longer 500 the
  service — results are coerced to JSON-safe values (`jsonable`) after PII
  masking.
- Enum argument matching now prefers the longest match, so
  `"count unpaid records"` cannot lose to a shorter enum value that happens
  to appear first in the list.
- A probe is no longer charged `UNCONSUMED_PENALTY` for the word it is NAMED
  after. The rescue compared a surface form (`status`) against a stem
  (`statu`) so it never fired, and `breakdown` — which merely declares
  `status` as an enum value of its `dimension` arg — out-scored
  `distinct_status` on that probe's own trigger `"status list"`.
- `neuralosd init` no longer writes preposition-only triggers (`"by"`,
  `"per"`) into the generated `breakdown` probe; a one-word trigger any
  question containing "by" can hit is a false-positive magnet.

**Refusals (false positives + false negatives)**
- Numeric arguments in prose: `"top 1,000 customers"` parses as 1000 (was 1)
  and is refused as `out_of_range` rather than silently clamped.
- `"last -5 days"` keeps its sign, so the range check refuses it instead of
  quietly answering about the last 5 days.
- Dates and times (`2026-10-31`, `31/10/2026`, `2026-10`, `14:30`) are
  stripped before argument extraction, so a date is never mistaken for a
  limit.
- Empty-result and strict-discard refusals now carry a bare-token
  `refusal_reason` (`no_results_produced` / `dropped_filter`) — they used to
  ship only a human sentence.
- `ask()` rejects non-string questions with a typed `TypeError` instead of
  failing deep inside tokenisation.

**Wire contract**
- `service.py` and `multi.py` no longer flatten refusals to
  `{"error": "no results produced"}`: the 422 body is the full envelope with
  `refusal_reason`, so `RefusalError.envelope["refusal_reason"]` works over
  HTTP exactly as it does in-process. Documented in `docs/sdk.md`.
- OpenAPI argument schemas emit `type: "string"` for enum args (was the
  invalid `type: "enum"`) while still carrying `values` **and** `enum`.

**New guards shipped with this release**
- `scripts/fp_fn_sweep.py` — precision/recall sweep over the shared refusal
  fixture: trigger recall, labelled answers, refusal precision, trigger
  collisions, noise stability. Exits nonzero on threshold violation. A trigger
  that routes to its probe and is then honestly refused for a missing required
  argument (`args_not_extractable`) counts as `arg_gated`, not as a miss — the
  router found the right probe; the question just did not say which one.
- `scripts/malformed_input_sweep.py` — 14 hostile-file cases (truncated
  cache, wrong top-level types, bad timestamps, non-JSON bodies, hostile
  strings).
- `.github/workflows/robustness.yml` — runs the suite plus both sweeps on
  ubuntu/macos/windows.


## 2.9.0 — Plasma EDR integration (all four layers)

- **L1**: `plasma:` source — reads events.jsonl, validates, counts parse errors. Dispatch order fixed.
- **L2**: security probes — critical_alerts, failed_logons, new_scheduled_tasks, events_by_type, events_by_host.
- **L3**: verified-answer loop on security data.
- **L4**: actuation with proof — dry-run → apply → undo.


## Documentation — production deployment guide

- Usage §12: honest production-readiness statement (solid / young /
  known gaps) + a 6-point operational checklist (token auth, golden
  bank + CI badge, SLO monitoring, drift audit, sidecar, trap bank).
- Usage §6e: streaming ingestion, federation gossip, roles management —
  with full command examples and bounded/lifetime guarantees.
- Reference: `neuralosd.watch` module; gossip and roles CLI in the
  command table; `resolve_role_instance` in service.
- Cookbook: production deployment checklist recipe (7 steps).

## 2.8.5 — gossip self-ingestion fix

- `gossip-pull` no longer self-ingests its own `gossip_state.json` as a
  fake peer (found live on the chinook instance). Stale self-ingested
  entries from previous runs are purged on load.

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
