# neuralOS — Complete Feature & Capability Matrix

Every feature and capability across the neuralOS ecosystem: the model, the
runtime (`neuralosd`), the hosting backends (BoxLite / Microsandbox), the
skills, and the integration surface.

---

## Core Framework

| Capability | What it does |
|---|---|
| `@probe` decorator | Turn any Python function into a typed, verified, routing-ready capability with triggers, grammar-caged args, PII fields, confidence gate, and execution tier |
| Deterministic fast path | Extract enum-caged args from the question and execute the probe directly — bypassing the model entirely for known patterns |
| Lexical retrieval (top-K) | Score the question against all probe triggers/names/descriptions; the model sees only the top-K most relevant probes, never the full menu |
| Possessive normalization | "Ireland's top customers" → "top customers in Ireland" — automatically rewritten before routing |
| Strong-match rank-walk | Walk retrieved probes in rank order; execute the first whose args are fully extractable AND whose lexical score is strong |
| Results gate | Refuse (exit 2 / HTTP 422) when no results are produced — never print stale data from a previous ask |
| Trigger collision linter | Simulate lexical retrieval for every trigger; flag HARD collisions (owner not in top-K: guaranteed mis-route) and soft (model disambiguates) |
| Per-probe confidence calibration | Learn per-probe accept thresholds from the golden bank — engine confidence is NOT calibrated to correctness by default |
| PII masking | Fields matching email/phone/ssn/iban/tax_id/passport are masked before results reach the model, cache, or caller |
| Audit trail | Every ask logged as JSONL: timestamp, question, normalized question, probe, confidence, latency, mode, cache status |
| TTL cache | Identical questions served from cache keyed by (normalized question, menu version) — invalidated on menu change |
| Menu generation | Auto-generate `needle_menu.json` from `@probe` declarations — grammar-caged args, triggers, descriptions |
| OpenAPI 3.1 generation | Every probe becomes a typed OpenAPI operation |
| MCP tool manifest | Every probe exposed as an MCP tool for AI agent hosts |
| Query catalog | Human-readable `CATALOG.md` listing every question the instance can answer |

---

## Probe Chains

| Capability | What it does |
|---|---|
| `@chain` decorator | Declare a named multi-step probe DAG |
| Dot-notation templates | `{{step_0.rows.0.genre}}` — extract values from previous steps into later args |
| Sequential execution | Each step runs in order; results accumulate in a context dict |
| Error propagation | A failed step raises ChainError with the step name and reason |
| Deep path resolution | `{{step_0.rows.0.genre}}` traverses nested dicts and list indices |

---

## Instance Routing

| Capability | What it does |
|---|---|
| Meta-selector | Route a question to the right instance using embedding similarity or lexical scoring |
| Instance registry | Discover, load, and hot-reload instances from a directory |
| Multi-tenant isolation | Each instance has its own probes, data, and state — no cross-contamination |
| Instance versioning | Menu version hash invalidates cache on any change |

---

## Guardrails

| Capability | What it does |
|---|---|
| HITL confirm store | Single-use tokens with TTL — risky probes pause for human approval |
| Grammar-caged arguments | The model fills args within declared constraints (enum, range, pattern) |
| Abstention | The model returns empty rather than guessing — the system refuses rather than prints wrong answers |
| Results gate | Never print results when none were produced for this question |
| Read-only SQL guard | The bridge driver rejects any non-SELECT statement |
| Row cap | Probes return at most N rows (default 25) to the model |
| Egress control | Sandboxes can have network fully disabled or allowlisted |
| Policy middleware | Per-caller allowlists gate probe families |

---

## Model (needle 3)

| Capability | Value |
|---|---|
| Parameters | 121M (2-bit SAN architecture, no FFN) |
| Weights | ~35 MB, bundled in the pip wheel |
| RAM | ~95 MB peak |
| Speed | ~1,600 tok/s prefill, ~830 tok/s decode (measured on M-series Mac) |
| Offline | Zero network at inference time |
| License | MIT (needle) / Apache-2.0 (neuralos pip package) |
| Embeddings | Same model returns text vectors — no second model needed |
| Abstention | Returns empty list when no tool matches — never hallucinates |
| Confidence | Learned calibration head — routing signal for guardrails |
| Grammar | Byte-level decode constraint from schemas — output always parses |
| Extraction | Declare a Pydantic shape, hand over messy text, get typed fields |
| Fine-tuning | Supported by the vendor (Cactus Compute) for domain specialization |

---

## Hosting Backends

| Capability | BoxLite | Microsandbox |
|---|---|---|
| Persistent disk | ✅ QCOW2, CoW | ✅ upper.ext4 |
| Stop/start persistence | ✅ files persist | ✅ files persist |
| Boot to serving | ~5–10s | ~5–15s |
| Clone (CoW) | ✅ near-instant | ✅ via snapshot restore |
| Live RAM snapshot | ❌ | ✅ 6.4s, 400MB |
| Fork (RAM-accurate) | ❌ | ✅ `--forked` |
| Port publication | ✅ local TCP | ✅ `-p HOST:GUEST` |
| Egress control | ✅ `allow_net` | ✅ `--net-rule` |
| Export/import | ✅ `.boxlite` archive | ✅ `.msb` archive |
| Windows | WSL2 only | **Native** (WHP, x64+ARM64) |
| Daemonless embed | ✅ library | ✅ library |
| REST server | ✅ `boxlite serve` | ✅ msb server |

---

## Verification Gates

| Gate | What it proves | Pass threshold |
|---|---|---|
| Model coverage | % of records parsing through Pydantic | ≥ 95% |
| Selection test | Correct probe picked for each of 3 phrasings | 3/3 per probe |
| Truth check | Relayed number == direct source query | 100% match |
| Invariants | Property checks (counts, sums, ranges) | all pass |
| Golden bank | Every seeded question produces results | 100% |
| Trigger lint | No HARD collisions (guaranteed mis-routes) | 0 hard |
| HITL confirm | Confirm flow works end-to-end | token → execute → result |
| Boot persistence | Instance loads correctly after stop/start | verified per deployment |

---

## Integration Surface

| Surface | Protocol | Consumers |
|---|---|---|
| REST API | HTTP JSON | Any HTTP client, curl, monitoring |
| OpenAPI 3.1 | HTTP + spec | Swagger UI, code generators, API gateways |
| MCP | JSON-RPC stdio | Claude Desktop, Cursor, any MCP host |
| Python SDK | import neuralosd | Any Python application |
| CLI | `neuralosd <command>` | Shell scripts, CI/CD, cron |
| A2A mesh | NATS/synapse | Cross-host, cross-org agent federation |

---

## Operational Features

| Capability | What it does |
|---|---|
| Upgrade with auto-rollback | Snapshot → upgrade → verify → auto-restore on failure |
| Backup with retention | Timestamped archives with keep-N pruning |
| Warm pool | Pre-cloned sandboxes ready for instant dispensing |
| Health/readiness | `/healthz` (liveness) and `/ready` (data layer check) endpoints |
| Audit trail | Every ask logged with full context, tamper-evident JSONL |
| Cache invalidation | Menu version in cache key — menu changes invalidate all entries |
| Hot reload | Registry re-imports probes without daemon restart |
| Metrics | Prometheus-compatible counters (asks, latency, cache hits, refusals) |
| Template versioning | Named templates with rollback archives |
| Gap mining | Refused/fuzzy questions → proposed triggers/probes (self-improving loop) |

---

## Infrastructure

| Capability | What it does |
|---|---|
| MicroVM isolation | Each box has its own kernel — exploits don't cross to host |
| Persistent disk | QCOW2/upper.ext4 survives stop/start |
| Copy-on-write clones | New boxes share the base disk — near-zero incremental cost |
| Process isolation | Each box runs independently — one crash doesn't affect others |
| Secret management | Host-side injection — secrets never enter the sandbox |
| Read-only mounts | Host directories mounted into boxes as read-only |
| Cross-host portability | Export/import archives between machines |
| Scheduler | Interval-based asks on background threads |
| Event hooks | File watchers, webhook ingress → probe execution |

---

## Deployment Patterns

| Pattern | What it does |
|---|---|
| Single-box analyst | One box, everything inside — prototyping and small teams |
| Template + clones | Cook once, clone N workers sharing the base disk (CoW) |
| Multi-source federation | Meta-selector routes each question to the right instance |
| Warm pool | Pre-cloned sandboxes ready for instant dispensing |
| Edge/IoT | needle on Raspberry Pi/phone — sensors → tools |
| Private RAG | Embeddings + extraction probes, zero cloud, PII-masked |
| Log monitoring | Log tail → probes → alerts |
| Ticket triage | Decision seam → route → probe answer |

---

## Data Source Support

| Source kind | `--source` format | Probes generated |
|---|---|---|
| CSV / TSV | `path/to/file.csv` | peek/filter/count/aggregate |
| MySQL / Postgres | `mysql://user:pass@host:port/db` | count/query/aggregate per table |
| SQLite | `sqlite:///path/to/db.sqlite` | same as above |
| JSON / JSONL | `path/to/file.jsonl` | peek/filter/count nested records |
| Log files | `path/to/app.log` | parse_tail/count_by_level/search |
| REST API | `https://api.example.com/data` | per-resource probes with auth |
| Excel / XLSX | `path/to/file.xlsx` | per-sheet peek |
| Directories | `path/to/dir/` | per-file-pattern probes |
| Unstructured text | `--source notes.txt --kind text` | extraction via model schema |
