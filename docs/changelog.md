# Changelog

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
