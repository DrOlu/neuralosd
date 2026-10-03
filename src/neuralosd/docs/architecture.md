# neuralOS Architectural & Design Manual

The complete architectural reference for the neuralOS instance kernel,
runtime, and deployment patterns. This document explains *why* the system
is designed the way it is, not just *how* to use it.

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [The Deterministic-First Principle](#2-the-deterministic-first-principle)
3. [Architecture Diagram](#3-architecture-diagram)
4. [The Instance Kernel](#4-the-instance-kernel)
5. [The Router Pipeline](#5-the-router-pipeline)
6. [The Model Layer](#6-the-model-layer)
7. [The Data Layer](#7-the-data-layer)
8. [The Guardrail Chain](#8-the-guardrail-chain)
9. [State Management](#9-state-management)
10. [Networking & Isolation](#10-networking--isolation)
11. [Security Model](#11-security-model)
12. [Performance Model](#12-performance-model)
13. [Deployment Patterns](#13-deployment-patterns)
14. [Failure Modes](#14-failure-modes)

---

## 1. Design Philosophy

neuralOS is built on one principle: **the model is a semantic resolver, not
a chat model.**

This means:
- The model reads messy human intent and emits a **typed call** — never prose
- The model **cannot hallucinate** because it never generates free-form text
  as an answer; it either calls a probe or returns empty
- The system layer (retrieval, verification, guardrails) is **deterministic
  software** that wraps the model and guarantees correctness
- The model is **replaceable** — the system layer works with any model that
  does tool selection

This is the opposite of chat-first agent frameworks (LangChain, AutoGPT)
where the model generates free-form text and the system hopes it's correct.
In neuralOS, the system layer is trusted and the model is constrained.

### The three trust boundaries

| Layer | Trust level | What it does |
|---|---|---|
| **System layer** (router, guardrails, verification) | Fully trusted | Deterministic code, tested, audited |
| **Data layer** (bridge, models) | Verified | Pydantic validates, SQL is parameterized |
| **Model layer** (121M selector) | Constrained | Grammar-caged, confidence-scored, can abstain |

The model can only: pick a probe from the menu, fill grammar-caged
arguments, or return empty. It cannot: execute code, access the network,
bypass the bridge, or generate answers outside the probe results.

---

## 2. The Deterministic-First Principle

Every ask follows this pipeline, where each step is tried before the next:

```
1. Normalize       deterministic (regex rewrite of possessives)
2. Cache           deterministic (hash lookup)
3. Retrieve        deterministic (lexical scoring, no model)
4. Fast path       deterministic (extract enum-caged args, execute directly)
5. Model fallback  the ONLY step that uses the model
6. Gate            deterministic (refuse if results are empty)
```

**The model is the fallback, not the primary path.** For known patterns
(country-caged queries, exact trigger matches, zero-arg probes), the system
resolves the intent without the model — faster, more reliably, and with
zero hallucination risk.

The model is needed only when the question doesn't match any known pattern —
and even then, it's constrained to the menu probes with grammar-caged
arguments.

---

## 3. Architecture Diagram

```
┌────────────────── Host Process ────────────────────────────────────┐
│                                                                     │
│  Caller (CLI / HTTP / SDK / MCP)                                    │
│    │                                                                │
│    ▼                                                                │
│  ┌── Router ──────────────────────────────────────────────────┐    │
│  │  normalize → cache → retrieve → fast path → model → gate   │    │
│  └──────┬──────────────────────────────────────────────────────┘    │
│         │                                                           │
│         ▼                                                           │
│  ┌── Guardrail Chain ─────────────────────────────────────────┐    │
│  │  calibration → PII mask → policy → HITL confirm → audit   │    │
│  └──────┬──────────────────────────────────────────────────────┘    │
│         │                                                           │
│         ▼                                                           │
│  ┌── Data Layer (bridge) ──────────────────────────────────────┐    │
│  │  SQL (parameterized) → Pydantic validation → row cap       │    │
│  │  → digest (small results to model, full rows to caller)    │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌── Engine (needle 3) ────────────────────────────────────────┐    │
│  │  121M params · 2-bit SAN · ~35 MB weights · ~95 MB RAM     │    │
│  │  byte-level grammar from schemas · calibrated confidence   │    │
│  │  abstention (empty list, not guess) · embeddings           │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. The Instance Kernel

An **instance** is a named set of probes + a data source + verification
gates. It is the deployable unit of neuralOS.

### 4.1 Instance directory layout

```
my_instance/
├── needle_menu.json       ← the menu: what the model can ask for
├── bridge.py              ← data layer: reads source, validates, returns
├── models.py              ← strict Pydantic models (from profiling)
├── instance.py            ← the agentic agent (deprecated CLI)
├── ask.py                 ← MANDATED entry point (structured retrieval)
├── serve.py               ← standard HTTP service
├── golden.json            ← golden question bank
├── truth.json             ← SQL cross-checks (database sources)
├── invariants.py          ← property checks
├── CATALOG.md             ← human "what you can ask"
├── openapi.json           ← OpenAPI 3.1 typed operations
├── mcp.json               ← MCP tool manifest
├── graph_edges.json       ← relationship layer (if discovered)
├── graph_bridge.py        ← runtime graph traversal
├── verify.py              ← verification runner
├── ask_audit.jsonl        ← per-ask audit trail (generated)
├── .ask_cache.json        ← TTL cache (generated)
└── verification.txt       ← phase-4 evidence
```

### 4.2 The menu (`needle_menu.json`)

The menu is the contract between the caller and the model. Each entry:

```json
{
  "name": "chinook_top_customers_in",
  "description": "Top customers of ONE named country (country copied verbatim)",
  "parameters": {
    "type": "object",
    "properties": {
      "country": {
        "type": "string",
        "enum": ["Argentina", "Brazil", ..., "USA"]
      }
    },
    "required": ["country"]
  },
  "triggers": [
    "top customers in Brazil",
    "top customers in France",
    "Ireland's top customers",
    "best customers in Brazil"
  ]
}
```

**The triggers are the routing signal.** The model (or the retrieval layer)
matches the question against these triggers to select the probe. Without
triggers, the model cannot reliably route — this is the #1 rule.

### 4.3 The bridge (`bridge.py`)

The bridge is the only component that touches the data source. It:

1. Connects to the source (parameterized, read-only)
2. Executes the probe's SQL/log-parse/API call
3. Validates every row through the Pydantic model
4. Caps results (ROW_CAP, typically 25)
5. Returns a standard envelope: `{returned, rows, validation, ...}`

**Bridge rules:**
- ALL queries are read-only SELECTs
- User input reaches SQL only via parameterized `%s` placeholders
- Validation failures are counted, not hidden
- Credentials are baked as constants, never model-facing arguments
- A self-healing connection survives idle timeouts and host sleep/wake

---

## 5. The Router Pipeline

The router is the deterministic pipeline that every ask flows through.
Each step is tried before the next; the model is step 5 of 6.

### 5.1 Pipeline steps

| Step | What happens | Model needed? |
|---|---|---|
| **1. Normalize** | Rewrite possessives ("Ireland's top customers" → "top customers in Ireland") from enum values | No |
| **2. Cache** | Check TTL cache keyed by (normalized question, menu version) | No |
| **3. Retrieve** | Lexical top-K: score question against probe triggers/name/description | No |
| **4. Fast path** | If rank-1 probe's required args are extractable with certainty, execute directly | No |
| **5. Model fallback** | Give the model the top-K subset; it picks and fills the call | **Yes** |
| **6. Gate** | If `results` is empty/None → refuse (exit 2 / HTTP 422) | No |

**Steps 1–4 resolve ~80% of questions without the model.** The model is
needed only for novel phrasings that the lexical retrieval can't pattern-match.

### 5.2 The fast path (step 4)

The fast path is the most important optimization. When the rank-1 probe's
required arguments can be extracted with certainty from the question
(e.g. a country name that matches the enum values), the probe is executed
directly — the model is bypassed entirely.

**Why this matters:**
- **Speed**: no model inference (~0 ms vs ~100–2000 ms)
- **Reliability**: the probe is called with exactly the right args, no
  selection error possible
- **Abstention**: if the args can't be extracted, the system skips to the
  next probe (or the model), never guesses

### 5.3 The rank-walk

When the rank-1 probe's args can't be extracted, the router walks the
retrieved probes in rank order and executes the first one whose args ARE
extractable. This prevents the "bare phrase routes to caged probe" failure
mode (verified live: "top customers" ranked the country-caged probe first
because its description overlapped more).

### 5.4 The strong-match rule

A probe with **no caged arguments** is ambiguous — any question could hit
it. The fast path only auto-executes such a probe if it's the rank-1
result AND its lexical score is strong (≥ ½ of rank-1's score). Otherwise,
the model fallback resolves it.

### 5.5 The results gate

After the model fallback, if `results` is empty/None, the router raises
`NoResults` — it **never** prints stale data from a previous ask. This is
the most important safety property: the caller never sees a wrong answer
presented as correct.

---

## 6. The Model Layer

### 6.1 The engine

The needle 3 engine is a **Simple Attention Network (SAN)** — no
feed-forward network, INT4-quantized from training, byte-level grammar
constraining every token. The model:

1. Reads the question + the probe menu
2. Selects the best-matching probe (or returns empty for no match)
3. Fills every argument from the question text
4. Returns a calibrated confidence score
5. Guarantees parseable output via the decode grammar

### 6.2 Key model properties

| Property | Value | Why it matters |
|---|---|---|
| Parameters | 121M | Small enough for CPU-only, on-device |
| Weights | ~35 MB (2-bit quantized) | Fits in any sandbox, no download needed |
| RAM | ~95 MB peak | Runs in a 1 GB sandbox |
| Speed | ~1,600 tok/s prefill, ~830 tok/s decode | Real-time on CPU |
| Abstention | Returns empty when no tool matches | Never hallucinates a tool call |
| Grammar | Byte-level, compiled from schemas | Output always parses |
| Confidence | Learned head, calibrated | Routing signal for the guardrail |
| Embeddings | Same model, separate method | Vector search without a second model |
| Offline | No network at inference | Data never leaves the box |

### 6.3 Known limitations

| Limitation | Mitigation |
|---|---|
| Tool selection fumbles on large menus (> 12 probes) | Structured retrieval (ask.py) narrows to top-K |
| Tool selection fumbles on novel phrasings | Deterministic fast path for known patterns |
| English-only tokenizer (breaks on Devanagari etc.) | Normalization layer; use a multilingual model for non-English |
| Confidence not calibrated to correctness | Per-probe calibration from golden bank |
| Trigger bloat degrades selection | Compact patterns, lint after menu changes |

---

## 6b. The Accounting Principle

> **Nothing is discarded silently.**

Every dangerous bug this runtime has produced was the same bug: *a confident
answer to a question that was not asked.* A filter named in the question that
no argument consumed. A grand total that counted rows the breakdown had
dropped. An argument clamped from 999 to 10 so the caller received a different
record than the one they asked for. In each case the output was plausible, the
parts looked internally consistent, and nothing said otherwise.

Accounting is the structural fix, and it is a *runtime mechanism*, not a
documentation claim:

| Vector | Mechanism |
|---|---|
| a filter the question named, unconsumed | `discarded.terms` |
| rows a probe excluded | `discarded.rows` (probe self-reports `skipped`) |
| parts that disagree with the whole | `grand_total` / `unaccounted` |
| an argument outside its declared range | `mode="refused"` + `discarded.out_of_range` |

Three properties make it hold:

1. **Probes self-account.** Every generated aggregate emits `rows_in`,
   `rows_counted` and `skipped`. The invariant `rows_in == rows_counted +
   sum(skipped)` is checkable from outside the probe.
2. **The ledger reaches the envelope.** A caller does not have to trust the
   number; it can see what was left out and decide. `--strict` turns the ledger
   into a refusal.
3. **Partial answers are never cached.** Memoizing one as if it were complete
   is how a dropped filter becomes permanent for the whole TTL.

The routing penalty (`UNCONSUMED_PENALTY`) and the ledger share one
implementation (`_unconsumed_for`) on purpose. Two copies of one rule drift,
and a drifted copy is a silent no-op — which is exactly what happened when
`extract_args` was defined twice in `router.py`: a fix written into the first
copy had no effect on any live code path while the source clearly showed it.

## 7. The Data Layer

### 7.1 The bridge

The bridge is the **only** component that touches the data source. It:

1. Connects (parameterized, read-only)
2. Executes the probe's query
3. Validates every row through Pydantic
4. Caps results (ROW_CAP)
5. Returns a standard envelope

**Bridge contract:**
- ALL queries are read-only SELECTs
- A read-only guard raises on non-SELECT statements
- Connection is self-healing (ping + reconnect on idle timeout)
- Credentials are constants, never model-facing
- Validation failures are counted, not hidden
- A retry on a fresh connection handles pipe-death between ping and execute

### 7.2 The Pydantic models

Models validate every row that flows through the bridge:
- Types from the profile (with constraints from observed ranges)
- `extra="forbid"` — rejects unexpected keys
- Aliases match the source's field names
- Datetime parsers handle multiple formats
- Enum fields validate against catalog constants

### 7.3 Result shaping

Probes return **small results** to the model:
- Counts and digests, not full rows
- ROW_CAP limits the number of rows
- Large payloads go to a stash file with a handle
- The full data is available to the caller, not the model

---

## 8. The Guardrail Chain

Every ask passes through ordered middleware:

| Order | Guardrail | What it does |
|---|---|---|
| 1 | **Normalization** | Rewrite possessives from enum values |
| 2 | **Calibration gate** | Confidence ≥ per-probe threshold (from golden bank) |
| 3 | **PII mask** | Fields matching email/phone/ssn/iban patterns are masked before results leave the guardrail |
| 4 | **Policy** | Per-caller allowlists, field-level redaction, egress rules |
| 5 | **HITL confirm** | Probes flagged `confirm=True` pause for human approval |
| 6 | **Audit** | JSONL append: timestamp, question, probe, confidence, latency |

**PII masking** uses a host-side MITM CA: the model sees a placeholder, and
the real value is substituted only at the transport layer. The secret never
enters the VM.

---

## 9. State Management

### 9.1 What persists

| State | Where | Survives stop/start |
|---|---|---|
| Installed packages | Box/sandbox QCOW2 disk | ✅ |
| Instance files | Box/sandbox disk or host volume | ✅ |
| Data (dump or live) | Box/sandbox disk or host volume | ✅ |
| Audit log | Box/sandbox disk or host volume | ✅ |
| Ask cache | Box/sandbox disk or host volume | ✅ |
| **Running processes** | ❌ (not in BoxLite; yes in Boxd) | Depends on runtime |

### 9.2 What doesn't persist

- Running processes (re-exec on start)
- In-memory state (conversation, agent state)
- Temporary files (unless in a mounted volume)

### 9.3 The persistence contract

```
Box start →
  1. Packages are still installed (disk layer)
  2. Instance files are still there (disk layer)
  3. Data is still loaded (disk layer or live connection)
  4. Services must be re-executed (start.sh or manual)
  5. Cache is valid (menu_version unchanged)
```

---

## 10. Networking & Isolation

### 10.1 BoxLite networking

Every box gets its own user-mode netstack (gvproxy):
- Outbound internet via NAT (full access by default)
- `allow_net` egress allowlist (per-domain/CIDR)
- Local TCP port publication (`ports=[(host, guest)]`)
- One-shot service tunnels
- DNS with configurable nameservers

**All boxes see the same internal IP** (`192.168.127.2`) because each has
its own isolated netstack. Boxes cannot see each other directly — connect
through the host.

### 10.2 Microsandbox networking

Per-sandbox /30 subnets from `172.16.0.0/12`:
- Each sandbox has a unique internal IP (but forks duplicate the parent's)
- `--net` profiles (public/private/host/all/none)
- `--net-rule allow@target` for fine-grained egress
- Egress bandwidth/packet limits
- TLS interception via built-in proxy
- Published ports: `-p HOST:GUEST`

### 10.3 Egress control

For data instances that need zero network at runtime:
- BoxLite: `allow_net=[]` or `NetworkSpec(mode="none")`
- MSB: `--net-default deny` or `--no-net`
- Bake dependencies into the image so no network is needed at runtime

---

## 11. Security Model

### 11.1 Isolation layers

| Layer | Mechanism | What it prevents |
|---|---|---|
| **Hypervisor** | KVM / HVF / WHP microVM | Kernel exploits reaching the host |
| **Jailer** | seccomp, namespaces, cgroups | VMM escape, resource abuse |
| **Bridge** | Read-only SQL guard, parameterized queries | SQL injection, data writes |
| **Router** | Grammar-caged args, results gate | Model hallucination, stale data |
| **PII mask** | Host-side CA substitution | Secrets entering the VM |
| **Egress control** | allowlist/deny | Data exfiltration |

### 11.2 Trust boundaries

```
┌── Trusted ──────────────────────────────────────────┐
│  Router (deterministic code)                        │
│  Bridge (parameterized SQL)                         │
│  Verification (golden bank, truth, invariants)      │
├── Constrained ─────────────────────────────────────┤
│  Model (grammar-caged, confidence-scored)           │
│  Probe execution (function calls, capped results)   │
├── Untrusted ────────────────────────────────────────┤
│  User input (normalised, never raw in SQL)          │
│  Model output (grammar-caged, confidence-gated)     │
│  Data in sandbox (isolated microVM)                 │
└─────────────────────────────────────────────────────┘
```

### 11.3 The PII boundary

PII fields (email, phone, SSN, IBAN) are masked **before** results reach
the model or the caller. The masking uses a host-side CA: the model sees
a placeholder, the transport layer substitutes the real value. The secret
never enters the sandbox.

---

## 12. Performance Model

### 12.1 Latency budget

| Phase | Latency | Notes |
|---|---|---|
| Normalize | < 1 ms | Regex rewrite |
| Cache lookup | < 1 ms | Hash + JSON |
| Retrieval (lexical) | < 1 ms | Token scoring |
| Fast path (deterministic) | < 1 ms | Direct probe call |
| Model inference | 100–2000 ms | Weights mmap + forward pass |
| Bridge query | 1–100 ms | Depends on source |
| **Total (fast path)** | **< 5 ms** | No model needed |
| **Total (model fallback)** | **100–2000 ms** | Model inference dominates |

### 12.2 Throughput

- Prefill: ~1,600 tok/s (measured on M-series Mac)
- Decode: ~830 tok/s
- Peak RAM: ~95 MB (model) + bridge overhead
- Per-sandbox overhead: ~178 MB (idle) to ~357 MB (under load)

### 12.3 Scaling

| Approach | When | How |
|---|---|---|
| **Clone** | Need N identical instances | CoW from template base |
| **Warm pool** | Latency-critical | Pre-create stopped boxes, start on demand |
| **Shared engine** | Multiple callers, same instance | One daemon, many HTTP clients |
| **Federation** | Multiple data sources | Meta-selector routes to the right instance |

---

## 13. Deployment Patterns

### Pattern 1: Single-box analyst (simplest)

```
┌── Box: analyst ─────────────────────────┐
│  neuralOS + MariaDB + instance files    │
│  serve.py :8877                         │
└─────────────────────────────────────────┘
```
One box, everything inside. Best for prototyping and small teams.

### Pattern 2: Template + clones (scaling)

```
┌── Template: neuralos-template ──────────┐
│  Base image + neuralOS (stopped)        │
└────────┬────────────────────────────────┘
         │ clone (CoW)
    ┌────┴────┬───────────┬───────────┐
    ▼         ▼           ▼           ▼
┌── worker-1 ─┐ ┌─ worker-2 ─┐ ┌─ worker-N ─┐
│ instances   │ │ instances   │ │ instances   │
└─────────────┘ └────────────┘ └────────────┘
```
Each clone shares the base disk (CoW). Independent execution.

### Pattern 3: Multi-source federation

```
┌── Meta-selector ─────────────────────────┐
│  route(question) → instance name         │
└──────┬──────────┬───────────┬────────────┘
       ▼          ▼           ▼
┌── finance ─┐ ┌─ ops ─────┐ ┌─ hr ────────┐
│ MariaDB    │ │ Postgres  │ │ CSV          │
│ probes     │ │ probes    │ │ probes       │
└────────────┘ └───────────┘ └─────────────┘
```
The meta-selector routes each question to the right instance. Each
instance has its own data source and probe set.

---

## 14. Failure Modes

| Failure | Detection | Recovery |
|---|---|---|
| **Stale results** | Results gate: empty check after model fallback | Refuse (exit 2); never print previous ask's data |
| **Selector fumble** | `type=call` with zero parsed calls | Results gate catches it; deterministic fast path bypasses |
| **Wrong probe selected** | Truth check: relayed number ≠ source query | Fix triggers; re-run selection suite |
| **Data source unreachable** | Connection error in bridge | Surface the error; never return empty as success |
| **Model inference wedge** | Client timeout on model ask | Drop the view/agent; next ask rebuilds |
| **Cache poisoning** | Menu version change invalidates cache | Menu version in cache key |
| **PII leak** | PII mask disabled or misconfigured | Default-on mask; audit log shows what was served |
| **Trigger collision** | Lint detects cross-probe routing | Reword, remove, or add distinguishing tokens |
| **Rootfs wiped** | Box recreate or image rebuild | Self-installing entrypoint; snapshot as rollback |
| **Port conflict** | Two services on same port | Change port or use a reverse proxy |

---

## 15. Design Decisions Record

| Decision | Rationale | Alternative considered |
|---|---|---|
| Deterministic-first (model as fallback) | Instant, provable, reversible; model errors are the #1 failure mode | Fine-tune the model (needs labeled data, can regress) |
| Grammar-caged decoding | Output always parses; no JSON validation needed | Post-hoc JSON validation (can fail silently) |
| Abstention over guessing | Empty result → refuse → improve; wrong answer → trust erodes | Always return best guess |
| Per-probe calibration | Engine confidence ≠ correctness; global gates are too coarse | Global confidence threshold (too blunt) |
| Results-gate (not function_calls gate) | `function_calls` is empty in 3.0.3 even on success | Branch on function_calls (silently passes on failure) |
| Lexical retrieval (not embedding index) | Zero dependencies, works offline, fast enough for ≤ 50 probes | Embedding index (tool_index_path doesn't materialize in 3.0.3) |
| MicroVM isolation (not containers) | Shared-kernel containers are escapeable; agents generate untrusted code | Docker containers (weaker isolation) |
| Deterministic chains (not model-planned) | Model-planned paths are probabilistic; declared DAGs are verifiable | Let the model plan multi-step execution |
| PII mask at host (not in VM) | Secrets never enter the sandbox; no leak surface | Inject secrets into the sandbox (leak surface) |


## Data access: in-process vs sidecar

The frozen binary is *sealed*: it carries a fixed set of libraries and cannot
import the host's packages. The **sidecar** is the escape hatch that keeps the
binary stable while allowing arbitrary libraries.

```
                 route / verify / serve           execute the probe
frozen binary  ───────────────────────────►  ┌──────────────────────────┐
  (stable)                                    │ in-process  (baked libs) │
                                              │      or                  │
                                              │ sidecar     (host python)│
                                              └──────────────────────────┘
                        JSON-RPC over stdin/stdout
```

Selection rules (see `neuralosd/_cmd/_common.py`):

1. **Whole-instance delegation** — `probes.py` itself cannot be imported here
   (a module-level import is missing) → the binary asks the sidecar for its
   menu and rebuilds every probe as a forwarding stub.
2. **Per-probe call-time retry** — the module imported fine but a *lazy*
   import inside the probe body failed → that single call is retried in the
   sidecar. This is the common case, and the easiest to get wrong: a
   load-time-only fallback silently never fires.
3. **Explicit** — a probe declared `tier="sidecar"` always delegates.

**Bootstrapping.** A sidecar needs a host Python. `neuralosd.provision` removes
even that assumption by driving uv, which ships its own CPython builds:

```
uv python install <ver>        (non-fatal — uv venv can fetch its own)
uv venv --python <ver> ~/.neuralosd/sidecar
uv pip install --python <env>/python neuralosd==<build> <extras>
```

The result is discovered automatically (PATH first, then the provisioned
environment), so a bootstrapped machine needs no configuration. Provisioning
is deliberately **explicit** — it touches the network, so it must never be
triggered from inside an answer. Version-pinning the environment to the
binary's own version keeps the protocol honest.

Two invariants worth preserving:

* the sidecar is started **lazily** — a fully-in-process instance never pays
  for a subprocess;
* a failure is **never cached** (`router._is_error_envelope`) — otherwise the
  user installs the missing library, retries, and receives the stale error.


## The model fallback

```
question
   │
   ├─ lexical retrieval (token overlap)  ──► deterministic execution
   │                                          (fast; the model is never loaded)
   │
   └─ nothing executable?
         ├─ no model configured ──► honest refusal
         └─ model configured
               ├─ retrieval found candidates? → offer those
               └─ nothing at all?             → offer the whole menu
                     │
                     └─ the model picks a real probe and fills its args,
                        then that probe runs in the data layer
```

Design notes worth keeping:

* **Results, not function_calls.** needle reports `function_calls: []` even on
  success; the answer lives in `results` (a list of JSON strings). Anything
  gating on `function_calls` silently sees nothing.
* **Attribution.** Because `function_calls` is empty, the bridge records which
  tool ran by wrapping each probe, so the envelope's `probe` field reflects
  what actually executed rather than whatever ranked first lexically.
* **One lock, everything.** Engine *construction* is as unsafe as inference.
  Serialising only `run()` was not enough — six concurrent constructions
  segfaulted the server. The model is a fallback, so serialising it costs
  little.
* **The model never invents an answer.** It can only select from the real
  probes; the probe does the reading.
