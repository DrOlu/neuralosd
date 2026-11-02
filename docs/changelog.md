# Changelog

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
