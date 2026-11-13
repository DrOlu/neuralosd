<div class="raft-hero" markdown="0">
<p class="raft-eyebrow">Verified agent runtime &middot; v2.9.16</p>
<h1>The model picks.<br>The runtime verifies.<br>The code answers.</h1>
<p class="raft-lede">neuralOSd is an offline, on-device data-answering runtime that refuses
what it cannot verify, accounts for what it excludes, and ships with its own tests.
Built for regulated environments where a wrong answer is worse than no answer.</p>
<div class="raft-actions">
<a class="raft-btn primary" href="usage/">Quick start</a>
<a class="raft-btn" href="cli/">CLI reference</a>
<a class="raft-btn" href="https://github.com/DrOlu/neuralosd">GitHub</a>
</div>
<div class="raft-badges">
<span class="raft-badge">Refuse-if-unknown</span>
<span class="raft-badge">Discard accounting</span>
<span class="raft-badge">Offline by default</span>
<span class="raft-badge">Ships with tests</span>
</div>
</div>

<div class="raft-grid">
<div class="raft-card"><div class="swatch coral">REFUSE</div><div class="body">
<h3>Weak match, no answer</h3>
<p>If the evidence does not clear the bar, the runtime refuses and names the reason instead of guessing.</p>
</div></div>
<div class="raft-card"><div class="swatch cyan">ACCOUNT</div><div class="body">
<h3>Nothing disappears quietly</h3>
<p>Every excluded row is counted and surfaced, so the answer's boundary is visible to the operator.</p>
</div></div>
<div class="raft-card"><div class="swatch lime">PROVE</div><div class="body">
<h3>Verified end to end</h3>
<p>Invariants, oracle checks and a packaged self-test suite run against the instance you deployed.</p>
</div></div>
</div>

<div class="raft-strip">An answer you can audit beats an answer that sounds confident.</div>

## Quick Start

```bash
pip install 'neuralosd[all]'
neuralosd init --source your_data.csv --name mydata --out ./mydata
neuralosd ask --instance-dir ./mydata "how many rows"
```

## Why it's different

Every other AI data tool optimises for *answering*. neuralOSd optimises for
*not answering when it can't verify*. That's the opposite design goal, and
it's what makes it safe for regulated environments.

| Every other tool | neuralOSd |
|---|---|
| The model generates an answer | The model **selects from a closed list** |
| If the answer looks wrong, add a prompt | If the match is weak, **refuse** |
| Verification = ask the model if it's sure | Verification = **accounting, invariants, oracle** |
| Errors are silent | Discards are **visible** |
| Deployment = "it should work" | Deployment = **verified end to end** |

## What's new in 2.8.1 — gossip + streaming

- **Federation gossip** — `mesh gossip-push/pull`: the fingerprint
  exchange becomes a protocol — validated blobs, capped counts, per-peer
  dedup. Your next probe is one another org already needed.
- **Streaming ingestion** — `neuralosd watch` tails logs into a bounded
  buffer and alerts on thresholds with webhooks.

## What's new in 2.9.8 — the pi build path, wired end to end

Four releases of fixing what only breaks on a real host (2.9.5 → 2.9.8):

- **Multi-instance doors stop colliding** (2.9.5) — two `serve
  --instances-dir` defects that corrupted fleet state fixed.
- **Every refusal is dated, and self-test traffic is out of the SLO**
  (2.9.6) — defect rows carry `ts`/`mode`/`refusal_reason`; `golden` and
  `trap` replays land in a separate ledger so the fleet refusal-rate SLO
  measures users, not the test suite.
- **The durable bridge actually ships — and loads** (2.9.7) —
  `pi_durable_bridge.mjs` was missing from every pip install AND could
  never load from site-packages (ESM resolves from the file's own path).
  It's packaged now, and staged automatically beside whichever
  node_modules holds @earendil-works/pi-durable, so `--durable` works
  the same from pip, binaries, and dev checkouts. A packaging
  completeness test keeps every loose file honest.
- **Build failures name their cause** (2.9.8) — a failed durable open
  (bad provider, missing sqlite) reports the bridge's own message
  instead of a downstream `submit: not open`.

The `pi` and `pi_durable` doctor checks now cover the whole path — and a
one-shot build through a custom models.json provider was proven live:
pi authored a verified new probe against the real workbook, banks
untouched, both banks replaying green.

## What's new in 2.9.4 — mesh hardening: preflight, provenance, trust

- **Preflight — "would you even answer this?"** — `Router.dry_run()` and
  `mesh preflight` run the decision half of the engine with the execution
  half removed: a neighbour (or your own tooling) learns `answered` /
  `refused` + `refusal_reason` + the score vector **without touching a
  row**. Fan-out over an estate collapses to N cheap probes + 1 real ask.
- **Signed, versioned verdicts** — every mesh answer and refusal carries
  `menu_version` / `gate_version` / `mesh_protocol_version` and a
  signature over the *sent* view (hmac-sha256 when a mesh key is set).
  Redaction happens before signing, so the signature always matches what
  was sent. Peers on a stale menu are detectable; tampering is detectable.
- **Trust classes** — `TrustPolicy` over `self` / `trusted` / `known` /
  `untrusted` decides what crosses the boundary: raw scores vs a coarse
  confidence band, filter ledger vs a labelled `"withheld"`. Strangers can
  no longer enumerate your probe menu from refusals, and capabilities
  advertise trigger *types*, never enum values.
- **Honest delegation** — a peer's refusal surfaces as a refusal (with its
  reason), answers are verified against what the peer advertised and
  stamped `verified: true` / `answered_unverified`, loops are cut by a
  visited set, chains by `MAX_HOPS=3`, and everything by a deadline.
- **Discovery with the router's own brain** — peers are shortlisted by the
  same scorer that routes local questions, filtered by preflight; plus
  bounded heartbeats with menu-drift detection and two-way gap gossip with
  honest per-cluster verdicts (`covered` / `extend_trigger` /
  `new_subject` / `new_probe`).
- **Release-engine fix** — component auto-updates now bump ALL FOUR version
  carriers (pyproject, runtime, SDK, npm) together. 2.9.3 was a
  dependency-only release (boxlite 0.12.0) that shipped between here and
  the mesh work; PyPI never reuses versions, so the mesh release is 2.9.4.

## What's new in 2.9.1 — robustness sweep

- **Refusals stay honest over HTTP** — the 422 body now carries the full
  envelope with a bare-token `refusal_reason`, so `RefusalError` works the
  same in-process and over the wire.
- **No silent wrong answers** — thousands separators, negative numbers,
  dates-as-limits, duplicate probe names and enum collisions are all caught
  or refused instead of guessed.
- **Results never 500 the service** — `Decimal`/`date`/`bytes` and cyclic
  structures are coerced or rendered safely.
- **Continuous guards** — precision/recall sweep, malformed-input sweep and
  a new robustness CI workflow across three operating systems.

## What's new in 2.9.0 — Plasma EDR integration

- **`plasma:` source connector** — reads events.jsonl from Plasma's
  WHIDS/krabsetw/identity sensors on Windows nodes; normalizes to the
  standard event shape; validation counts parse errors.
- **Security probes** — `critical_alerts`, `failed_logons`,
  `new_scheduled_tasks`, `events_by_type`, `events_by_host` — with
  birth traps.
- **Actuation with proof** — `neuralosd actuate` (dry-run → plan_id →
  apply → undo), every step journaled with who/when/what/why/rollback.
  Five actions: block_ip, isolate_host, disable_user, rotate_credential,
  quarantine_file.
- Dispatch order fix: prefix sources checked before extension sources.

## What's new in 2.8.0 — joins everywhere + roles

- **Joins for every source kind** — `--extra-source` now takes sqlite,
  Excel, JSON, authenticated APIs (credentials caged), or CSV; named joins
  (`--on prices:region=region`) are deterministic nested loops.
- **Roles management** — `neuralosd roles` manages allow-lists and
  row-level filters with validation and a journal; `serve` enforces them
  per request via the `X-NeuralOS-Role` header.
- Fixed: the RBAC row cage left a stale `count` next to filtered rows;
  `--extra-source` was silently dropped by a CLI dest-name mismatch.

## What's new in 2.7.0 — durable builds + doctor

- **Pi Durable** — `neuralosd build "<capability>" --durable`: the build
  conversation runs in a crash-resumable Pi Durable harness with
  exactly-once submissions; `--resume` continues after a crash.
  `neuralosd pi durable --status|--steer|--stop` for operator sessions.
- **`neuralosd doctor`** — health-checks every integration in one command.
- **Faster ops on big audits** (bounded reads) and **smarter build
  prompts** (bridge inventory + probe names + data sample up front).

## What's new in 2.6.0 — natural language builds

- **pi as the builder** — `neuralosd build "average shipping cost per unit
  by region"`: the pi coding agent writes the probe code, neuralosd
  verifies it (banks untouched, instance loads, banks replay green —
  anything else is restored). Default model: z-ai/glm-5.3-flash.
- **pi as the operator** — `neuralosd pi ask "why did refusals go up?"`:
  natural-language ops over the closed command list.
- `neuralosd pi --setup` verifies pi, the model, and a live ping.

## What's new in 2.5.0 — agent2agent + durable

- **synapse-gateway, for real** — the mesh client now speaks the documented
  gateway REST surface (`/api/mesh/*`, Bearer token from
  `REACTORPRO_GATEWAY_TOKEN` or gateway.env). Ask any peer:
  `neuralosd mesh send --target grip-001 "summarise the build"`. Durable
  tasks, mailbox, trust — and `mesh setup` to verify it all.
- **Temporal durable execution** — the loop's recurring work (golden bank,
  gap mining, shadow evaluation, snapshots) as durable activities:
  `neuralosd temporal --setup`, `temporal worker`, `temporal
  schedule-create --cron "0 2 * * *"`.

## What's new in 2.4.0 — sources, proof, trust

- **agent-browser connector** — `init --source browser:<url>` turns a web
  page into a profiled source through the
  [agent-browser](https://github.com/vercel/agent-browser) CLI:
  accessibility-tree snapshots become rows, roles become caged enums, and
  `neuralosd browser --session` drives persistent sessions (open / snapshot
  / click / fill / network). Verified answers now reach the web.
- **Authenticated API sources** — `init --source api:<url>` with credential
  **caging**: the instance stores only a secret *reference*
  (`scrt:` / `op:` / `env:`), resolved at ask time — the secret never
  touches disk.
- **Multi-source instances with caged joins** — `--extra-source` + `--on`:
  one instance, several row sources, one deterministic join.
- **Value-level oracles** — golden cases now verify the ANSWER with typed
  shapes (count / percent / ratio / date / text) and invariants. Wrong
  value ≠ pass, even when routing is perfect.
- **Snapshot pinning** — `snapshot --label q3` freezes hashed observations;
  `ask --as-of q3` answers from the pin after the source changes.
  Answers become reproducible evidence.
- **RBAC / row-level caging** — `acl.json` gives roles probe allow-lists
  and row filters; `ask --role analyst` serves only what that role may see.
- **Refusal-rate SLO + fleet dashboard** — `slo` and `fleet` quantify the
  menu-gap signal; exit 1 on breach.
- **Golden-bank CI badge** — `badge` emits a shields.io endpoint + a GitHub
  Actions gate; bank health lives in the README, outside the box.
- **Cluster-driven synonyms** — `synonyms` proposes triggers from gap
  clusters (audit score vectors + caged enums as ownership evidence);
  human-approved, applied to derived metrics, emitted as edit suggestions
  for probes.py.
- **Streaming windows** — log instances get a caged `recent_events(minutes)`
  probe: ask over "the last hour".

## What's new in 2.3.0 — the loop grows up

Ten closing moves that turn the verify-answer loop into a self-improving
one. Full details in [Usage §6b](usage.md#6b-close-the-loop-v230) and the
[changelog](changelog.md).

1. **Probe birth traps** — every reason-installed probe synthesizes its
   misuse trap into `traps.json` and **proves the refusal** with the live
   router before the loop reports success. A capability ships guarded, or
   the install fails loudly.
2. **Refusal clustering** — `gaps` clusters refusals into patterns with
   cost framing: *"this gap cost you 412 refused queries across 38
   phrasings."* Prioritize by what users repeatedly want.
3. **Negative-feedback ingestion** — `neuralosd feedback` turns a
   thumbs-down into a bank candidate with the user's correction attached;
   `--apply` promotes only **proven** candidates.
4. **Schema-similarity probe transfer** — `neuralosd transfer` proposes a
   schema sibling's probes with the golden bank translated and re-oracle
   flagged. Proposals never auto-install.
5. **Oracle provenance** — `golden --record` stores who verified a value,
   when, from what source, how; `golden --receipts` prints the evidence
   chain. The golden bank is an evidence chain now, not just a test file.
6. **Shadow mode** — `reason --shadow` installs probes that cannot serve
   traffic; `shadow --evaluate/--promote` promotes only on oracle
   agreement. Kills the "generated probe confidently wrong" class.
7. **Consensus** — `reason --consensus-model` requires a second, DIFFERENT
   model to independently pick the same ids; disagreement refuses the
   install.
8. **Auto-deprecation** — `neuralosd mothball` demotes probes nobody
   queries (kept on disk, reinstatable). Tight menus route better.
9. **Schema-drift re-audit** — `neuralosd drift` re-profiles a changed
   source and flags the probes it breaks *before* they fail at runtime.
10. **Federated gap learning** — `gaps --export-fingerprints` /
    `--ingest-fingerprints`: peers on the mesh share anonymized token-pair
    hashes + counts, never the questions. Capability discovery,
    crowd-sourced, privacy-preserving.

## What's new in 2.1–2.2 — more sources, deeper Excel

- **SQLite connector** — `neuralosd init --source app.db` profiles a
  database with stdlib `sqlite3` (zero deps, works in the frozen binary).
- **Deep Excel** — formulas, every sheet, and **named ranges** profiled;
  `sheets` / `named_ranges` / `formula_scan` probes scaffolded.
- **Log sources** — `syslog:<path>` (RFC3164/5424, stdlib only),
  `journal:[unit]` (journald), `wevtutil:<Channel>` (Windows Event Log) —
  all normalized to `{ts, host, app, level, message}` events.
- **Auto-trap synthesis** — every registered probe gets its misuse trap in
  `traps.json` at birth (`NEURALOSD_AUTO_TRAPS=0` disables).
- **Cross-instance bridge fix** — no instance can inherit another's data
  layer.

Also in recent releases: the [`neuralosd_sdk`](sdk.md) Python/JS clients,
bearer-token auth on `serve`, and the import auditor (`neuralosd audit`).
