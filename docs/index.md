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
