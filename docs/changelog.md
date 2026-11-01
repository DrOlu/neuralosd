# Changelog

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
