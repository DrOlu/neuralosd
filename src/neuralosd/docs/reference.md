# neuralOS API Reference

Complete reference for every class, function, parameter, and attribute
in the `neuralosd` package.

---

## Table of Contents

- [neuralosd (top-level)](#neuralosd-top-level)
- [neuralosd.probe](#neuralosdprobe)
- [neuralosd.router](#neuralosdrouter)
- [neuralosd.instance](#neuralosdinstance)
- [neuralosd.chains](#neuralosdchains)
- [neuralosd.meta](#neuralosdmeta)
- [neuralosd.hitl](#neuralosdhitl)
- [neuralosd.mcp](#neuralosdmcp)
- [neuralosd.engine](#neuralosdengine)
- [neuralosd.registry](#neuralosdregistry)
- [neuralosd.memory](#neuralosdmemory)
- [neuralosd.scheduler](#neuralosdscheduler)
- [neuralosd.metrics](#neuralosdmetrics)
- [neuralosd.adapters](#neuralosdadapters)
- [neuralosd.lint](#neuralosdlint)

---

## neuralosd.sidecar

Runs an instance's probes in the **host** Python, so a frozen binary can use
libraries it does not carry. Speaks newline-delimited JSON-RPC on
stdin/stdout.

| | |
|---|---|
| `main(argv)` | CLI entry point (`neuralosd-sidecar`) |
| `Sidecar(instance_dir)` | serves one instance's probes |
| `Sidecar.handle(req)` | `ping` / `menu` / `call` / `shutdown` |
| `load_probe_functions(dir)` | import `probes.py`, return the probe functions |
| `probe_meta(fn)` | serialisable view of a probe declaration |

Protocol — one JSON object per line:

```jsonc
// request
{"id": 1, "method": "call", "params": {"probe": "pdf_pages", "args": {}}}
// response
{"id": 1, "ok": true,  "result": {"pages": 2}}
{"id": 1, "ok": false, "error": "unknown probe: 'ghost'"}
```

A `{"event": "ready"}` (or `"error"`) line is emitted before any response.

## neuralosd.sidecar_client

| | |
|---|---|
| `sidecar_command()` | resolve a helper: `$NEURALOSD_SIDECAR` → PATH → provisioned → `python3 -m neuralosd.sidecar` |
| `SidecarClient(instance_dir, command=None, timeout=180)` | long-lived subprocess |
| `.start()` / `.close()` | lifecycle (context manager supported) |
| `.call(probe, args)` / `.menu()` / `.ping()` | protocol methods |
| `SidecarError` | raised on not-found / died / timeout / remote error |

`$NEURALOSD_SIDECAR=off` (also `0`, `false`, `no`, `none`, `-`) disables the
sidecar entirely.

## neuralosd.provision

Creates a sidecar environment with uv — including a Python, if the machine has
none.

| | |
|---|---|
| `find_uv()` | `$NEURALOSD_UV`, PATH, then common install dirs |
| `sidecar_home()` | `~/.neuralosd` (`$NEURALOSD_HOME` overrides) |
| `sidecar_dir()` | `<home>/sidecar` |
| `sidecar_executable()` | `<home>/sidecar/{bin,Scripts}/neuralosd-sidecar` |
| `is_provisioned()` | does that executable exist? |
| `requirements(packages, version)` | the pip requirement list |
| `provision(python, packages, force, uv, version)` | create it (idempotent) |
| `status()` | dict for the CLI to report |
| `ProvisionError` | uv missing, uv failed, or a useless environment |

### CLI

```bash
neuralosd sidecar --status
neuralosd sidecar --setup [--with a,b] [--python 3.12] [--force]
```

### Environment variables

| Variable | Effect |
|---|---|
| `NEURALOSD_SIDECAR` | helper command; `off` disables delegation |
| `NEURALOSD_UV` | path to `uv` |
| `NEURALOSD_HOME` | where the provisioned environment lives |

## neuralosd (top-level)

### `__version__`

```python
neuralosd.__version__  # "1.0.0"
```

### Exports

```python
from neuralosd import (
    probe,              # @probe decorator
    enum_arg,           # enum-caged argument helper
    pattern_arg,        # pattern-caged argument helper
    int_arg,            # integer argument helper
    ProbeMeta,          # probe metadata dataclass
    Router,             # the ask pipeline
    NoResults,          # raised when nothing is produced
    Instance,           # multi-probe container
    ChainRunner,        # probe DAG runner
    chain,              # @chain decorator
    ChainError,         # chain execution error
    MetaSelector,       # instance routing
    ConfirmStore,       # HITL confirm store
    ConfirmRequired,    # raised when confirmation is needed
)
```

---

## neuralosd.probe

### `@probe(description, triggers, args, pii, conf_gate, tier, name, confirm)`

Decorator that turns a function into a routing-ready capability.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `description` | `str` | required | What the probe does. Used by retrieval and catalog. |
| `triggers` | `List[str]` | required | Phrasings that route to this probe. MANDATORY. |
| `args` | `Dict[str, Dict]` | `None` | Argument specs (see arg helpers below). |
| `pii` | `List[str]` | `None` | Field names containing PII (masked before output). |
| `conf_gate` | `float` | `None` | Per-probe confidence override (from calibration). |
| `tier` | `str` | `"in-process"` | Execution tier: `"in-process"`, `"sandbox:boxlite:NAME"`, or `"sandbox:msb:NAME"`. |
| `name` | `str` | function `__name__` | Override the probe name. |
| `confirm` | `bool` | `False` | Require HITL confirmation before execution. |

**Returns:** the decorated function with `._probe` attribute (ProbeMeta).

### Arg helper functions

#### `enum_arg(values, required=True)`

```python
enum_arg(["USA", "UK", "Nigeria"])           # required
enum_arg(["USA", "UK"], required=False)      # optional
```

| Parameter | Type | Description |
|---|---|---|
| `values` | `List[str]` | Allowed values. The router matches these against the question verbatim (case-insensitive). |
| `required` | `bool` | If True, the arg must be extracted or the fast path skips. |

#### `pattern_arg(pattern, required=True)`

```python
pattern_arg(r"albums (?:by|does|of|from)\s+(.+)")
```

| Parameter | Type | Description |
|---|---|---|
| `pattern` | `str` | Regex with one capture group. The captured value becomes the argument. |

#### `int_arg(minimum, maximum, default=None)`

```python
int_arg(1, 25, default=10)     # required=False, default=10
int_arg(1, 100)                # required=True
```

#### `arg(spec)`

Raw dict escape hatch for custom argument specs.

### `ProbeMeta`

Dataclass attached to every `@probe`-decorated function as `._probe`.

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Probe name (defaults to function `__name__`). |
| `description` | `str` | What the probe does. |
| `triggers` | `List[str]` | Phrasings that route to this probe. |
| `args` | `Dict[str, Dict]` | Argument specifications. |
| `pii` | `List[str]` | PII field names for masking. |
| `conf_gate` | `float` | Per-probe confidence threshold. |
| `tier` | `str` | Execution tier. |
| `confirm` | `bool` | Whether HITL confirmation is required. |
| `function` | `Callable` | The decorated function. |

---

## neuralosd.router

### `class Router`

The ask pipeline. Routes questions to probes via deterministic fast path
or model fallback.

#### `Router(probes, model_fallback=None, cache_file=..., audit_file=..., pii_mask=True, ttl=3600, name="instance")`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `probes` | `List[Callable]` | required | @probe-decorated functions. |
| `model_fallback` | `Callable` | `None` | `f(normalized, retrieved_metas) → results`. Called when the fast path can't resolve. |
| `cache_file` | `str` | `.ask_cache.json` | TTL cache file path. |
| `audit_file` | `str` | `ask_audit.jsonl` | Audit log path. |
| `pii_mask` | `bool` | `True` | Mask PII fields before returning results. |
| `ttl` | `int` | `3600` | Cache TTL in seconds. |
| `name` | `str` | `"instance"` | Instance name (used in menu version hash). |

#### `Router.ask(question, k=8, full=False, use_cache=True) → Dict`

Execute the full pipeline for one question.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `question` | `str` | required | The natural-language question. |
| `k` | `int` | `8` | Top-K probes for retrieval. |
| `full` | `bool` | `False` | Bypass retrieval (use full menu). |
| `use_cache` | `bool` | `True` | Check cache before executing. |

**Returns:** `Dict` — the standard envelope:
```python
{
    "ask_id": str,           # unique per ask
    "ts": float,             # timestamp
    "question": str,         # original question
    "normalized": str,       # possessive-normalized question
    "probe": str,            # probe name that answered
    "menu_version": str,     # menu hash
    "mode": str,             # "deterministic" | "retrieval" | "full-menu"
    "confidence": float,     # model confidence (None for deterministic)
    "latency_ms": int,       # total latency
    "cached": bool,          # True if served from cache
    "results": Any,          # probe output (list, dict, or value)
    "error": str,            # only present on failure
}
```

**Raises:** `NoResults` if nothing was produced.

#### `Router.menu() → List[Dict]`

Generate the needle_menu.json-compatible menu from the registered probes.

#### `Router.openapi(title=None) → Dict`

Generate an OpenAPI 3.1 spec from the registered probes.

#### `Router.lint(top=8, max_triggers=40) → Dict`

Run the trigger collision linter. Returns `{"hard": [...], "soft": [...], "bloat": [...]}`.

#### `Exception: NoResults(envelope)`

Raised when nothing was produced for a question. The `.envelope` attribute
contains the error details (never stale data). Also raised by a **strict
refusal** and by an out-of-range argument — `results` is `None` and `error`
explains why.

#### `Exception: ArgOutOfRange(argname, value, lo, hi)`

Raised when an integer argument's value falls outside its declared `min`/`max`.
Attributes: `.argname`, `.value`, `.lo`, `.hi`.

Arguments are **never clamped**. `user 999` against `1..10` used to return the
record for `user 10` — a different record, silently. The router records the
violation during its candidate walk and, if no candidate can serve the question,
refuses with `mode="refused"` and a `discarded.out_of_range` entry.

#### `UNCONSUMED_PENALTY = 5.0`

Subtracted per **named-but-ignored filter value** when choosing between
executable candidates. Kept above the per-trigger weight (4.0) so that ignoring
one word a candidate could have consumed is enough to lose to a probe that
consumes it. Without it, `"total revenue by region"` matched the flat
`total_revenue` (trigger `"total revenue"`, overlap 2) and returned one grand
total with `region` dropped.

#### `ARG_MATCH_BONUS = 3.0`

Added per extracted argument value present in the question.

#### `mask_pii(x, deep=True) → Any`

Mask by key name **and** (when `deep`) by value shape, recursively.
`deep=False` restores key-name-only behaviour. Key hints live in `PII_HINTS`;
value shapes in `SECRET_PATTERNS` (JWT, AWS, GitHub, OpenAI, Slack, PEM,
`Bearer`, connection strings). `AGGRESSIVE_PATTERNS` (long hex) is off unless
`NEURALOSD_MASK_AGGRESSIVE=1`.

Response masking is **not** storage masking — see `_storage_mask`.

#### `_storage_mask(x) → Any`

The masker applied to everything written to the cache and the audit log, at
every depth, regardless of `pii_mask`. Key hints + `SECRET_PATTERNS` +
`PII_VALUE_PATTERNS` (email, phone). `pii_mask=False` governs the *response*;
the cache expires, the audit log does not.

---

## neuralosd._cmd.scrub

### `run(args) → int`

Sanitize the state already on disk. Masking at write time protects *future*
records; this fixes the ones already written.

```bash
neuralosd scrub --instance-dir ./inst --dry-run
neuralosd scrub --instance-dir ./inst
neuralosd scrub --instance-dir ./inst --purge-cache
```

Uses the **storage** masker (these records are on disk, so response rules do not
apply). Reports which secret shapes it found — by name and count, never by
value — and how many records it rewrote.

## neuralosd.instance

### `class Instance`

A named set of @probe capabilities with optional model fallback and
state management.

#### `Instance(name, probes, model_fallback=None, state_dir=None, pii_mask=True, ttl=3600)`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | required | Instance name (used in menu version hash). |
| `probes` | `List[Callable]` | required | @probe-decorated functions. |
| `model_fallback` | `Callable` | `None` | `f(normalized, retrieved_metas) → results`. |
| `state_dir` | `str` | `"."` | Directory for cache and audit files. |
| `pii_mask` | `bool` | `True` | Enable PII masking. |
| `ttl` | `int` | `3600` | Cache TTL in seconds. |

#### `Instance.ask(question, **kw) → Dict`

Execute the full pipeline. See `Router.ask` for parameters and return shape.

#### `Instance.menu → List[Dict]`

Property. Returns the generated needle_menu.json-compatible menu.

#### `Instance.lint(top=8, max_triggers=40) → Dict`

Trigger collision report. Returns `{"hard": [...], "soft": [...], "bloat": [...]}`.

#### `Instance.golden_run(golden: Dict) → int`

Run every golden question through the router. Returns the number of passes.

#### `Instance.openapi(title=None) → Dict`

Generate an OpenAPI 3.1 spec.

#### `Instance.export(path: str)`

Export the instance as a portable archive.

---

## neuralosd.chains

### `class ChainRunner`

Executes declared probe DAGs with templated args.

#### `ChainRunner(probes_by_name: Dict[str, Callable])`

| Parameter | Type | Description |
|---|---|---|
| `probes_by_name` | `Dict[str, Callable]` | Map of probe name → function. |

#### `ChainRunner.register(name: str, steps: List[Dict])`

Register a named chain.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Chain name. |
| `steps` | `List[Dict]` | Each step: `{"probe": name, "args": {…}}`. |

#### `ChainRunner.run(name: str, initial_args: Dict = None) → Dict`

Execute the chain. Returns `{"chain": name, "steps": [...], "latency_ms": …}`.

### `resolve_template(value, context) → Any`

Replace `{{step_name.path.to.field}}` with values from previous results.
Supports dot-notation deep access into nested dicts and list indices.

### `@chain(name, steps)`

Decorator to register a chain (same as `register` but on the function).

---

## neuralosd.meta

### `class MetaSelector`

Routes questions to the right instance.

#### `MetaSelector(instances, embed_fn=None)`

| Parameter | Type | Description |
|---|---|---|
| `instances` | `Dict[str, Dict]` | `{name: {"description": str, "examples": [str, ...]}}`. |
| `embed_fn` | `Callable` | `text → list[float]` (needle embeddings). If None, uses lexical scoring. |

#### `MetaSelector.build()`

Pre-compute example embeddings (call once after construction).

#### `MetaSelector.route(question: str) → str`

Return the best instance name for the question.

---

## neuralosd.hitl

### `class ConfirmStore`

HITL confirm tokens with TTL expiry.

#### `ConfirmStore(ttl=300)`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ttl` | `int` | `300` | Token expiry in seconds. |

#### `ConfirmStore.create(probe_fn, args, reason) → Dict`

Create a pending confirmation. Returns `{"confirm_token": str, "probe": str, "reason": str, "ttl": int}`.

#### `ConfirmStore.confirm(token: str) → Dict`

Execute the confirmed probe. Returns the probe result.
Returns `{"error": "invalid or expired"}` for bad/expired tokens.

#### `ConfirmStore.pending_count() → int`

Number of pending confirmations.

---

## neuralosd.mcp

### `handle(request: Dict, instance_dir: str) → Dict`

Handle a single JSON-RPC request (MCP protocol).

| Method | Description |
|---|---|
| `initialize` | Handshake (returns protocol version + capabilities). |
| `tools/list` | Returns all probes as typed tools. |
| `tools/call` | Executes a probe. `params.name` = probe name, `params.arguments` = kwargs. |

### `main()`

Start the MCP server on stdio. Reads JSON-RPC from stdin, writes to stdout.

---

## neuralosd.engine

### `class EnginePool`

Manages the loaded engine and per-instance views.

#### `EnginePool(build_timeout=180, ask_timeout=120)`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `build_timeout` | `int` | `180` | View build timeout (seconds). |
| `ask_timeout` | `int` | `120` | Model inference timeout (seconds). |

#### `EnginePool.fallback(instance_name, tools_by_name) → Callable`

Returns a `fallback(normalized, retrieved_metas) → results` callable for
the given instance. The view is built lazily and cached per instance.
A wedged view is dropped for rebuild on the next ask.

#### `EnginePool.embed(text: str) → List[float]`

Embed text using a shared tool-less view (needle 3 embeddings).

#### `EnginePool.stats() → Dict`

Returns `{"views": [...], "builds": int, "last_build_s": float}`.

---

## neuralosd.registry

### `class Registry`

Discovers and loads instances from a directory.

#### `Registry(instances_dir: str)`

| Parameter | Type | Description |
|---|---|---|
| `instances_dir` | `str` | Directory containing instance subdirectories. |

#### `Registry.discover() → Dict[str, Dict]`

Scan for instance dirs (must contain `probes.py`). Returns `{name: {"dir": str, "probes": List}}`.

#### `Registry.load(name: str) → Dict`

Load a single instance by name.

#### `Registry.reload(name: str) → Dict`

Re-import an instance's probes module (hot reload).

---

## neuralosd.memory

### `class MemoryStore`

Remember/recall backed by needle embeddings.

#### `MemoryStore(state_dir: str, embed_fn: Callable)`

| Parameter | Type | Description |
|---|---|---|
| `state_dir` | `str` | Directory for memory files. |
| `embed_fn` | `Callable` | `text → list[float]`. Needle 3's `embed()` method. |

#### `MemoryStore.remember(instance: str, text: str, meta=None) → Dict`

Store a text with its embedding vector.

#### `MemoryStore.recall(instance: str, q: str, k: int = 5) → List[Dict]`

Recall the top-k most similar stored texts.

---

## neuralosd.scheduler

### `class Scheduler`

Interval-based asks on a background thread.

#### `Scheduler(state_dir: str, ask_fn: Callable)`

| Parameter | Type | Description |
|---|---|---|
| `state_dir` | `str` | Directory for schedule persistence. |
| `ask_fn` | `Callable` | `f(instance, question) → envelope`. |

#### `Scheduler.add(name, every_s, instance, question) → Dict`

Add a scheduled ask.

#### `Scheduler.remove(name: str) → int`

Remove a schedule. Returns the number removed.

#### `Scheduler.list() → List[Dict]`

List all schedules.

#### `Scheduler.start()`

Start the background scheduler thread.

#### `Scheduler.stop()`

Stop the scheduler thread.

---

## neuralosd.metrics

### `class Metrics`

Thread-safe counters with Prometheus text rendering.

#### `Metrics.inc(name: str, value: float = 1.0)`

Increment a counter.

#### `Metrics.get(name: str) → float`

Read a counter value.

#### `Metrics.render() → str`

Render all counters as Prometheus text format.

---

## neuralosd.adapters

### `get_adapter(tier: str, **kwargs) → adapter or None`

Factory function. Returns an adapter for the given tier string, or None
for in-process execution.

| Tier format | Adapter |
|---|---|
| `sandbox:boxlite:BOX_NAME` | `BoxLiteAdapter` |
| `sandbox:msb:SANDBOX_NAME` | `MSBAdapter` |
| anything else | `None` (in-process) |

### `class BoxLiteAdapter`

| Method | Description |
|---|---|
| `exec_probe(code: str) → Dict` | Run Python code inside the box via REST. |
| `__init__(serve_url, api_key, box_name)` | Configure the adapter. |

### `class MSBAdapter`

| Method | Description |
|---|---|
| `exec_probe(code: str) → Dict` | Run Python code inside the sandbox via `msb exec`. |
| `__init__(sandbox_name)` | Configure the adapter. |

---

## neuralosd.lint

### `class TriggerLinter`

Trigger collision linter — simulates lexical retrieval for every trigger
and flags cross-probe routing.

#### `TriggerLinter(menu: List[Dict], top: int = 8)`

| Parameter | Type | Description |
|---|---|---|
| `menu` | `List[Dict]` | The needle_menu.json content. |
| `top` | `int` | `8` | Top-K size used by the retrieval front-end. |

#### `TriggerLinter.lint() → Dict`

Returns `{"hard": [(owner, trigger, wrong)], "soft": [...], "bloat": [...]}`.

- **HARD**: owner not in top-K — guaranteed mis-route. Fix immediately.
- **soft**: owner in top-K but not rank-1 — model disambiguates with context.

---

## neuralosd.cli

### CLI commands

```bash
neuralosd init --source <path> --name <name> [--out <dir>] [--packages "…"]
neuralosd deploy --instance-dir <dir> --name <name> [--backend boxlite|msb] [--port N]
neuralosd ask --instance-dir <dir> "question"
neuralosd serve --instance-dir <dir> [--port 8877]
neuralosd lint <needle_menu.json> [--top N]
neuralosd golden [--dir <instance_dir>]
neuralosd truth [--dir <instance_dir>]
neuralosd invariants [--dir <instance_dir>]
neuralosd calibrate [--golden golden.json] [--base URL] [--out calibration.json]
neuralosd gaps <audit.jsonl> [--menu needle_menu.json] [--out proposals.json]
neuralosd openapi <needle_menu.json> [--agent NAME]
neuralosd diff <old_profile.json> <new_profile.json> [--menu needle_menu.json]
neuralosd backends
```

Every subcommand accepts standard flags. See `neuralosd <command> --help`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NEEDLE_TELEMETRY` | `1` | Set `0` to disable anonymous usage counts. |
| `DO_NOT_TRACK` | `0` | Set `1` to disable all tracking. |
| `NEURALOS_MASK_PII` | `1` | Set `0` to disable PII masking. |
| `NEURALOS_CACHE_TTL` | `3600` | Ask cache TTL in seconds. |
| `NEURALOSD_TOKEN` | — | Bearer token for the REST API. |
| `NEURALOSD_PORT` | `8420` | Default daemon port. |
| `NEURALOSD_INSTANCES` | `~/.neuralosd/instances` | Instance directory. |
| `NEURALOSD_STATE` | `~/.neuralosd/state` | State directory. |
| `NEURALOS_LAB` | `~/neuralos-lab` | Lab root for venv and archives. |
| `NEURALOS_DSN` | — | Database DSN override (database instances). |
