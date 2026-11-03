# neuralOSd

**The verified agent runtime.** An offline, on-device data-answering runtime
that **refuses what it cannot verify**, **accounts for what it excludes**,
and **ships with its own tests**. The model picks; the runtime verifies; the
code answers.

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
