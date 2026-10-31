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

## What's new in 2.0.6

- **SDK layer** — `neuralosd_sdk` (zero-dependency Python client) and a
  zero-dep JavaScript client: typed `ask()`, retries with backoff, refusals
  as exceptions. See the [SDK guide](sdk.md).
- **Token auth on `serve`** — `--auth-token` / `$NEURALOSD_API_TOKEN`
  requires a bearer token on every endpoint except liveness
  (`/healthz`, `/ready`); constant-time comparison.
- **Instance import audit** — `neuralosd audit` AST-parses every instance
  module and classifies imports as `bundled` / `host-only` /
  `instance-local` / `missing-everywhere` against the runtime and the
  sidecar. Exit code gates instance creation; `--root` scans a whole fleet.
- **Sidecar delegation, complete** — works for classic menu-only instances
  (no `probes.py` needed), for imports nested in helper functions, and
  `"tier": "sidecar"` is honored from menu entries.
- **sqlite3 bundled in frozen binaries** — menu bridges that
  `import sqlite3` load on every platform; the CLI now names the real
  import failure instead of a misleading hint.
- **Menu-instance tier honored** — `"tier": "sidecar"` in
  `needle_menu.json` is the classic-layout escape hatch.
- **Test gate in CI** — unit tests + the CLI smoke suite run on every PR.
