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
- [neuralosd.pisources](#neuralosdpisources)
- [neuralosd.synapse](#neuralosdsynapse)
- [neuralosd.durable](#neuralosddurable)
- [neuralosd.banks](#neuralosdbanks)
- [neuralosd.loop](#neuralosdloop)
- [neuralosd.logsources](#neuralosdlogsources)

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

Run the trigger collision linter. Returns `{"hard": [...], "soft": [...], "bloat": [...],
"trigger_spam": [(probe, cluster_size, sample_trigger), ...]}`.

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

## neuralosd.derived

### `class DerivedMetric`

A ratio declared as DATA. Validated, interpreted, and computed by this module —
nothing a model emits is executed.

```python
DerivedMetric(name, kind="ratio", numerator, denominator, scale="ratio",
              description="", triggers=[], question="", min_coverage=0.6,
              provenance={})
```

`numerator`/`denominator` are **quantity ids** from the closed inventory
(`<probe>.<field>` or `<probe>.rows[].<field>`). `validate()` raises
`DerivedError` on a bad name, an unknown `kind`/`scale`, a missing id, identical
ids, or a `min_coverage` outside 0..1.

### `inventory_from_observations(observations) -> List[Quantity]`

Build the CLOSED list from `{probe_name: returned_dict}`. Observed rather than
declared, so every id is a field some probe really returned. Identifiers and
calendar parts are excluded — summing customer ids is arithmetically valid and
semantically meaningless, and a model asked to pick from a list will pick one.

### `resolve(quantity, observations) -> float | None`

Turn a quantity into a number. The only place a number is produced.

### `evaluate(metric, observations, by_id=None) -> dict`

Deterministic. Raises `DerivedError` for an unknown id, an unresolvable value,
or a zero denominator. Returns `{metric, numerator{id,value}, denominator{...},
ratio, percent, value, unit, formula, _tool, _derived}`.

### `expectation_for(question) -> "lte_1" | "any"`

Read off the question, never from the model — so the model cannot relax its own
acceptance test. A "share of" question cannot legitimately produce more than 1.

### `check_expectation(value, expectation) -> str | None`

Returns a reason string when the value violates the expectation.

### `as_probe(metric, observations) -> Callable`

Wrap a spec as a routable probe, closing over a snapshot of the observations.
Sets `min_coverage` on the ProbeMeta.

### `load(path)` / `save(path, metrics)`

Read/write `derived.json`. A missing file is an empty registry, not an error.

## neuralosd.reasoning

### `needs_escalation(question, served_probe, probe_text=None) -> str | None`

The deterministic gate. Returns the uncovered ratio qualifier, or None. Coverage
is tested on the qualifier's CONTENT WORDS, so a description reading "revenue
share percentages" covers "share of".

### `class OllamaMapper(model, url, timeout)`

The reasoning model as a closed-list classifier. `temperature=0`, `seed=42`,
`format=json`. `available()` checks both reachability and that the model is
pulled. `map_ids(...)` raises `MapperError` when Ollama is unreachable.

### `validate_pick(pick, inventory) -> (clean, reason)`

The anti-hallucination gate. Note the `"null"` STRING: deepseek-r1 replies
`{"numerator": "null"}` rather than JSON null, and that is treated as a decline.

### `propose(question, served, observations, mapper, oracle=None, tolerance=1e-6, check_stability=True, scale="percent") -> Proposal`

The full loop with **no side effects**. Checks: `closed_inventory`,
`stable_across_two_calls`, `ids_in_closed_list`, `computes`,
`expectation_<x>`, `matches_outside_oracle`. `Proposal.ok` is True only when no
check failed.

### `triggers_from_question(question) -> List[str]`

Triggers derived from the question, never from a model. A model-written trigger
list is how a new probe hijacks its siblings.

### `snapshot_routes(questions, ask)` / `compare_routes(before, after, exempt)`

The baseline must be THIS router's own prior behaviour. Comparing against
another engine's answers reports that engine's pre-existing disagreements as
damage done by the new metric.

### `backtest(cases, ask)` / `install(metric, instance_dir)`

Replay against explicit golden expectations; write `derived.json` without
touching `probes.py`.

## neuralosd.instance_audit

AST-based import audit for instances: closes the "frozen binary can't see
instance imports" gap at birth.

- `collect_imports(path)` — every top-level module name imported anywhere in
  a module (top-level AND function-level, try/except fallbacks, aliases;
  relative imports skipped).
- `collect_instance_imports(instance_dir)` — union over
  bridge/instance/probes, minus instance-internal cross-imports.
- `instance_local_modules(instance_dir)` — sibling `.py` files and local
  packages; these resolve via the dir's sys.path entry and are classified
  `instance-local`, never missing.
- `importable_set(exe, modules)` — EMPIRICAL check: which modules can a
  given python actually import (find_spec, one subprocess).
- `classify_imports(mods, bundled, host, instance_local)` — each module →
  `bundled | host-only | instance-local | missing-everywhere`. With an
  unknown host, unproven modules are NEVER passed silently.
- `audit_instance(dir, bundled, host)` / `audit_root(root, host_exe)` —
  report dicts; `ok` is False iff anything is missing-everywhere.

## neuralosd_sdk

The zero-dependency client for `neuralosd serve` (Python; a matching ESM
client lives at `clients/javascript/neuralosd-client.mjs`).

- `NeuralOSDClient(base_url, token=None, timeout=30, max_retries=2,
  backoff=0.5)` — token auth via `Authorization: Bearer`.
- `.ask(question) -> AskResult` — typed result (probe, results, mode,
  cached, latency_ms, raw envelope). Retries connection errors and 5xx with
  exponential backoff; **4xx is never retried** (retrying a refusal would
  be asking for a hallucination).
- `.health()` / `.ready()` / `.openapi()`.
- Errors: `AuthError` (401), `RefusalError` (422 — the instance honestly
  declined; `.envelope` carries the reason), `TransportError`.

## neuralosd.menu_adapter

### `probes_from_menu(instance_dir, module_names=None, errors=None)`

Lift a `needle_menu.json` into `ProbeMeta` objects so the same router serves it.
Pass `errors` (a dict) to collect why a module failed to import — without it, a
missing database driver looks exactly like a menu whose names do not match its
functions.

### `derived_probes(instance_dir, observations=None)` / `observe(instance_dir)`

Load `derived.json` as routable probes; call every menu probe once and record
its output.

### `load_menu_instance(instance_dir) -> Instance | None`

The `Instance` for a classic needle-menu directory.

### `Router` — coverage

`ProbeMeta.min_coverage` (default `None`) is the fraction of the probe's best
trigger the question must contain. Token-overlap scoring reports 2-of-8 the same
as 2-of-2; coverage tells them apart. `None` keeps the original scoring, so
every existing probe is unaffected.

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

Trigger collision report. Returns `{"hard": [...], "soft": [...], "bloat": [...],
"trigger_spam": [(probe, cluster_size, sample_trigger), ...]}`.

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

## neuralosd.plasma

Plasma EDR connector (v2.9.0) — read events.jsonl from Plasma's
WHIDS/krabsetw/identity sensors on Windows nodes.

| | |
|---|---|
| `parse_plasma_event(line)` | one PlasmaEvent JSONL row → validated event dict or None |
| `parse_plasma_events(path, cap)` | read the store; returns {rows, parse_errors, truncated} |
| `plasma_rows(path, cap)` | convenience: just the rows (for probe functions) |

## neuralosd.actuate

Actuation with proof (v2.9.0) — named, journaled, reversible actions.

| | |
|---|---|
| `actuate(action, target, reason, operator, instance_dir, ...)` | one step: dry-run (mints plan_id), apply (requires plan_id from a seen dry-run), or undo |
| `ACTIONS` | block_ip, isolate_host, disable_user, rotate_credential, quarantine_file |
| journal | actuation_journal.jsonl — every step with who/when/what/why/rollback |

## neuralosd.pidurable

Pi Durable driver (v2.7.0; bridge packaging + staging v2.9.7, open-error
propagation v2.9.8) — durable, resumable build conversations.

| | |
|---|---|
| `DurableBuilder(storage, model, provider, spawn, cwd)` | line-JSON driver over the shipped Node bridge; `open` (raises `SystemExit` with the bridge's own message on failure — a bad provider says `provider <name> unavailable`, not a downstream symptom), `submit` (exactly-once requestId), `wait`, `status`, `close` |
| `stable_request_id(instance_dir, question)` | the idempotency key for a build |
| `build_durable(...)` (pisources) | durable build with the full verification/restore pipeline; storage survives failures for --resume |
| `pi_durable_available()` / `bridge_script_path()` | node + npm packages + the packaged bridge; what `doctor`'s `pi_durable` check calls |
| `pi_durable_command(storage, model, provider, cwd)` | node argv; stages the bridge into `<dir-of-node_modules>/.neuralosd-bridge/` (found by walking up from `cwd`, default the instance dir) so its ESM imports resolve from pip installs; falls back to the packaged path; the stage is content-compared against the packaged copy every build |

## neuralosd.watch

Streaming ingestion (v2.8.1) — bounded buffer, threshold rules, webhooks.

| | |
|---|---|
| `RingBuffer(path, cap)` | bounded JSONL event file; `append` (accepts single event or list), `read(limit)` |
| `tail_new_lines(path, state)` | offset-based tail with rotation detection; returns only NEW lines |
| `evaluate_rules(rules, events, now)` | threshold rules with subset-match filters and sliding windows; unparsable ts excluded |
| `post_webhook(url, payload, poster)` | honest True/False; never kills the loop |
| `run_cycle(instance_dir, sources, rules, ...)` | one poll cycle: ingest → buffer → rules → webhook |

## neuralosd.pisources

The pi coding agent as builder and operator (v2.6.0).

| | |
|---|---|
| `pi_command(text, model, provider, session)` | headless argv: `--print --no-session`, extensions/MCP/skills/context-files off |
| `pi_run(argv, cwd, execute)` | execute in the instance dir; missing pi = error naming the install |
| `setup(model, provider, which_fn, runner)` | pi on PATH + model resolves + live one-shot ping |
| `build_prompt / build_capability(question, instance_dir, execute)` | builder: contract-carrying prompt, then judge (banks byte-identical, instance loads, banks green) with full restore on any failure |
| `operator_prompt / operate(question, instance_dir, execute)` | operator: the closed command list, audited subprocess runs |

## neuralosd.synapse

Synapse-gateway mesh protocol (v2.9.4 — preflight, provenance, trust,
verification over the /api/mesh/* wire).

| | |
|---|---|
| `SynapseHTTP(gateway, timeout, token, token_path, retries)` | Bearer token from `REACTORPRO_GATEWAY_TOKEN` or the gateway.env file; never stored in instances. Retries only provably-idempotent calls (GET, keyed POST), backoff over retryable codes, `Idempotency-Key` header from taskId |
| `Deadline.from_ms(ms)` | shared deadline; bounds every socket timeout (`max(0.1s, min(timeout, remaining))`) |
| `agents / discover / dispatch` | the peer directory + synchronous ask (reply read payload.text → payload.output; error envelopes raise with the mesh code) |
| `create_task / get_task / cancel_task / retry_task / mailbox` | durable async tasks — caller-minted idempotent taskId |
| `health / status / trust / preflight` | operators' read-only surface + the peer's would-you-answer verb |
| `MeshAgent(instance_name, router, transport)` | the instance's mesh face: handle, preflight, register, heartbeat, gossip |
| `MeshAgent.handle(question, trust, mesh_protocol_version)` | verdict with versions + signature; protocol 2 → `incompatible` / `mesh_protocol_mismatch` |
| `MeshAgent.preflight(question, trust)` | `Router.dry_run` published: would/probe/refusal_reason, no probe run, signed |
| `MeshAgent.register()` | versioned registration blob — network-free; publishing is explicit |
| `MeshAgent.heartbeat(prev)` / `heartbeat_loop(max_beats, publish)` | menu-drift detection; bounded loop, annotated publish failures |
| `MeshAgent.capabilities() / digest()` | what the mesh may see: triggers + arg types, never enum values or vocab |
| `TrustPolicy.for_level(level)` | self / trusted / known / untrusted → score, candidate, ledger exposure + delegation rights |
| `answer_hash(body) / sign_envelope(body, key) / verify_signature(body, key)` | deterministic hash over the sent view; hmac-sha256 or sha256 integrity-only; `(ok, bare_reason)` |
| `verify_peer_answer(body, expected_*)` | is this peer answer what it advertised? bare reasons (`unknown_probe`, `stale_menu`, `gate_version_mismatch`, …) |
| `discover_peers(transport, question, self_id, limit, preflight)` | the router's own scorer, pointed outward: published triggers > skill names; self excluded; refusers dropped when preflighting |
| `delegate_to_peer(transport, peer, question, visited, max_hops, deadline, trust)` | honest delegation: refusals stay refusals, answers verified vs advertised, loop/hop/deadline rails, legacy `response`/`peer` keys preserved |
| `refusals_from_audit(path) / cluster_gaps(gaps) / export_fingerprints(clusters)` | gossip pipeline — questions never leave the edge, only sha256 pairs |
| `explain_fingerprints(fingerprints, own_gaps)` | OWNER-SIDE inversion: strangers holding the fingerprints recover nothing |
| `propose_triggers(router, refused, min_count)` | advisory trigger proposals with honest verdicts: covered / extend_trigger / new_subject / new_probe |
| `diff_menus(own_menu, peer_menu, own_id, peer_id)` | shared / only_local / only_peer / drifted triggers per probe |

## neuralosd.durable

Temporal durable execution for the loop (v2.5.0).

| | |
|---|---|
| `ACTIVITIES` / `run_activity(name, instance_dir)` | golden_run, gaps_mine, shadow_evaluate, snapshot_pin, drift_audit — plain JSON-able functions, no server needed |
| `build_worker(instance_dir, task_queue)` | temporalio worker (honest error without the SDK) |
| `schedule_create_args(...)` | the exact Temporal CLI call for a recurring loop schedule |
| `setup(which_fn, runner, has_temporalio)` | three honest checks: CLI, server, SDK |

## neuralosd.websources

agent-browser connector (v2.4.0) — the browser as a data source.

| | |
|---|---|
| `browser_rows(url, cap, runner)` | open + accessibility snapshot → rows `{role, name, ref, depth}`; refs are the action handles |
| `browser_session_op(session, op, ...)` | tier 2: named-session ops (open / snapshot / click / fill / type / network / text); `--session` pins login state |
| `setup_browser_tool(which_fn, installer, runner, prompt_fn, assume_yes)` | consent-gated install + live snapshot ping (`neuralosd browser --setup`); honest failures for no-Node, declined, off-PATH-after-install, ping-fail |

## neuralosd.apisources

Authenticated API sources with credential caging (v2.4.0).

| | |
|---|---|
| `resolve_secret(ref, runner)` | `env:` / `scrt:` / `op:` references resolved at ask time — never stored |
| `api_rows(url, header_name, secret_ref, cap, fetcher)` | fetch + normalize JSON to rows (list, single-list envelope, or single object) |

## neuralosd.oracles

Typed answer verification (v2.4.0) — the golden bank checks the ANSWER.

| | |
|---|---|
| `check_answer(shape, expected, actual)` | typed rules + invariants: count ≥ 0, percent 0..100, date parses, text casefold/strip |
| `extract_answer(results, path)` | dotted-path walk or bounded numeric/string leaf scan |

## neuralosd.banks

Behavioural banks — the golden (positive) and traps (refusal) files, plus
the v2.3.0 feedback/provenance lifecycle.

| | |
|---|---|
| `synthesize_trap_for(probe_meta, traps_path)` | append the probe's misuse trap (`"<first trigger> <probe name>"`, `expect_refusal`); idempotent, gated by `NEURALOSD_AUTO_TRAPS` (default on). Skips honestly when the raw name cannot carry a valid trap |
| `auto_traps_enabled()` | the gate (`NEURALOSD_AUTO_TRAPS`) |
| `ingest_feedback(instance_dir, question, verdict, correction, by, ts)` | thumbs-down → candidate: `bad` = golden candidate (correction attached, awaiting verified oracle), `invalid` = trap candidate |
| `apply_feedback(instance_dir)` | promote candidates with PROOF — traps only when the router already refuses; golden with live routing recorded and the oracle flagged UNVERIFIED |
| `record_golden(instance_dir, q, expect_probe, value, by, source, method, ts)` | append a golden case WITH its oracle receipt (who / when / source / how) |
| `receipts(instance_dir)` | the printable evidence chain; receipt-less cases say so |

## neuralosd.loop

The loop machinery (v2.3.0) — clustering, federation, shadow, transfer,
hygiene, drift.

| | |
|---|---|
| `cluster_gaps(gaps, threshold=0.5)` | cluster refusals by domain-token Jaccard; clusters carry size, member questions, common terms, and cost framing |
| `export_fingerprints(clusters)` | anonymized token-PAIR hashes + counts — questions never leave the edge |
| `ingest_fingerprints(clusters, peer_blob)` | merge a peer's fingerprints → `mesh_count` / `mesh_note` per cluster |
| `close_probe_birth(metric, instance_dir)` | synthesize the new probe's misuse trap and PROVE the refusal; receipt in `metric.provenance["birth_trap"]` |
| `shadow_install / shadow_report / shadow_evaluate / shadow_promote` | shadow-mode lifecycle: not routable until it agrees with its oracle (age + runs + threshold) |
| `propose_transfer(from_dir, to_dir, min_sim, out_path)` | schema-similarity proposal: reconfirmed shared probes with translated golden (re-oracle flagged) + proposed missing probes; never auto-installs |
| `mothball / mothballed_names / reinstate` | menu hygiene: demote stale probes (never empties the menu), reinstate on demand |
| `audit_drift(instance_dir, src)` | re-profile a changed source; flag name-caged and enum-caged probes it breaks |
| `gossip_push(transport, blob, peers, origin)` | publish validated fingerprints via gateway mailbox; blob validated BEFORE send |
| `gossip_collect(inbox_dir)` | collect + validate peer blobs from the inbox; malformed dropped, counts capped, deduped per peer |
| `gossip_peer_counts(inbox_dir)` | fp → best peer count (for mesh_count enrichment) |

## neuralosd.logsources

Log source connectors — every source normalizes to
`{ts, host, app, level, message}` event rows so the standard generated
probes work unchanged.

| | |
|---|---|
| `parse_syslog_line / parse_syslog_file(path, cap)` | RFC3164 / RFC5424; unparsable lines are counted (`parse_errors`), never silently dropped |
| `journal_rows(cap, unit, runner)` | journald via `journalctl -o json`, newest first, bounded |
| `wevt_rows(channel, cap, runner)` / `parse_wevt_xml(text)` | Windows Event Log via `wevtutil /f:XML`; BOM-aware decode (wevtutil redirects as UTF-16), namespace/declaration tolerant |
| `decode_tool_output(data)` | BOM-aware + NUL-heuristic decoding of native tool output |
| `log_cap_default()` | `NEURALOSD_LOG_CAP` (default 10000) |

## neuralosd.cli

### CLI commands

```bash
neuralosd init --source <path|prefix> --name <name> [--out <dir>] \
    [--extra-source prices.csv --on region=region] \
    [--since 1h] [--header H --secret-ref env:VAR]
neuralosd browser --session acme snapshot|click|fill|network ...
neuralosd snapshot --instance-dir <dir> --label q3
neuralosd slo --instance-dir <dir> [--window-hours 24 --max-refusal-rate 0.4 \
    --min-asks 10]
neuralosd fleet <dirs|globs> [...] [--max-refusal-rate 0.4 --min-asks 10]
neuralosd badge --instance-dir <dir> --out badge.json [--actions-out wf.yml]
neuralosd synonyms --instance-dir <dir> [--audit a.jsonl] [--apply proposals.json]
neuralosd ask --instance-dir <dir> "q" [--as-of q3] [--role analyst]
neuralosd deploy --instance-dir <dir> --name <name> [--backend boxlite|msb] [--port N]
neuralosd ask --instance-dir <dir> "question"
neuralosd serve --instance-dir <dir> [--port 8877] [--auth-token T]
neuralosd lint <needle_menu.json> [--top N]
neuralosd golden [--dir <instance_dir>]
neuralosd golden --dir <dir> --record --question "q" --expect-probe P \
    --value V --by WHO --source SRC --method HOW     # with oracle receipt
neuralosd golden --dir <dir> --receipts              # the evidence chain
neuralosd truth [--dir <instance_dir>]
neuralosd invariants [--dir <instance_dir>]
neuralosd calibrate [--golden golden.json] [--base URL] [--out calibration.json]
neuralosd gaps <audit.jsonl> [--menu needle_menu.json] [--out proposals.json]
neuralosd gaps <audit.jsonl> --export-fingerprints fp.json    # mesh payload
neuralosd gaps <audit.jsonl> --ingest-fingerprints peer_fp.json
neuralosd feedback --instance-dir <dir> --question "q" \
    --verdict bad|invalid [--correction "…"] [--by WHO]
neuralosd feedback --instance-dir <dir> --apply      # promote proven candidates
neuralosd mothball --instance-dir <dir> [--age-days 180] [--apply]
neuralosd mothball --instance-dir <dir> --reinstate <probe>
neuralosd drift --instance-dir <dir> --source <new_source>   # exit 1 = re-audit
neuralosd transfer --from <dirA> --to <dirB> [--min-sim 0.6] [--out FILE]
neuralosd shadow --instance-dir <dir> [--evaluate] [--promote]
neuralosd reason --instance-dir <dir> "question" [--oracle N] \
    [--consensus-model M] [--shadow] [--install]
neuralosd openapi <needle_menu.json> [--agent NAME]
neuralosd diff <old_profile.json> <new_profile.json> [--menu needle_menu.json]
neuralosd backends
neuralosd mesh setup|agents|send|task-create|task-status|task-cancel|task-retry|mailbox|trust
    [--gateway http://127.0.0.1:3000]
neuralosd mesh discover [SKILL] [--preflight --json]    # score the directory
neuralosd mesh preflight <instance-dir> "question"      # exit 0 answer / 2 refuse
neuralosd mesh delegate <agent-id> "question" [--visited a,b --max-hops 3
    --deadline-ms 30000 --trust trusted --preflight]    # exit 0 only if verified
neuralosd mesh gaps [--audit ask_audit.jsonl --min-count 2 --json]  # advisory
neuralosd mesh verify [FILE] [--key KEY]                # stdin; exit 0/1
neuralosd mesh register [--publish --json]              # blob is network-free
neuralosd mesh serve [--publish --heartbeat]
neuralosd mesh ask "question" [--delegate --preflight --max-hops 3
    --deadline-ms 30000 --trust trusted]                # exit 2 on refusal
neuralosd temporal --setup | --run-activity NAME | --worker | --schedule-create
    [--instance-dir . --task-queue neuralos --cron "0 2 * * *"]
neuralosd pi --setup | ask "question" [--model z-ai/glm-5.3-flash]
neuralosd build "<capability description>" --instance-dir <dir> \
    [--durable --resume] [--expect "question"]
neuralosd roles --instance-dir <dir> --op set|list|show|remove \
    [--role R --allow P1,P2 --row-filter "col=in:v1,v2"]
neuralosd watch --instance-dir <dir> --source syslog:/path \
    --rules rules.json [--once | --interval 30] [--webhook URL]
neuralosd mesh gossip-push --peers "a,b" [--audit audit.jsonl]
neuralosd mesh gossip-pull --inbox ./gossip_inbox
neuralosd actuate --instance-dir <dir> --action block_ip \
    --target <ip> --reason "..." --operator NAME [--apply --plan ID]
neuralosd doctor --instance-dir <dir>
```

`init` sources: `plasma:<path>` (Plasma EDR), `*.csv`, `*.tsv`, `*.xlsx`/`*.xlsm` (deep: formulas, all
sheets, named ranges), `*.json`/`*.jsonl`, `*.db`/`*.sqlite` (stdlib
sqlite3), `syslog:<path>`, `journal:[unit]`, `wevtutil:<Channel>`, anything
else (contract template). See
[Usage §3.0](usage.md#30-neuralosd-init-sources).

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
| `NEURALOSD_MIN_ASKS` | `10` | Smallest number of asks in the SLO window that can breach. Below it `refusal_rate()` reports `insufficient_data: true` and `breach: false` (the rate is still reported), and `slo`/`fleet` render the instance `n/a` and exit 0. `--min-asks` overrides it. |
| `NEURALOS_LAB` | `~/neuralos-lab` | Lab root for venv and archives. |
| `NEURALOS_DSN` | — | Database DSN override (database instances). |
| `NEURALOSD_AUDIT_FILE` | — | Audit ledger for SELF-TEST traffic (`golden`/`truth` replays). Absolute path, or relative to the instance's state dir. Default when unset: `ask_audit.selftest.jsonl` — the user ledger `ask_audit.jsonl` never receives replay rows. The CLI `--audit` flag overrides this, which overrides the self-test default. |
| `NEURALOSD_PI_PROVIDER` | `openrouter` | pi provider for `build` / `pi ask` / `reason`. Any provider the pi CLI resolves (built-in or a custom `~/.pi/agent/models.json` entry). |
| `NEURALOSD_PI_MODEL` | `z-ai/glm-5.3-flash` | pi model id. `--model`/`--provider` flags override these. |
