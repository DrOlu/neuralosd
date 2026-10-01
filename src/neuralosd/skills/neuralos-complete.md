---
name: neuralos-complete
description: >-
  The complete, all-inclusive neuralOS operations manual — install on any
  platform (macOS/Linux/Windows), configure any data source (CSV, JSON, logs,
  databases, APIs, Excel, unstructured text), build instances end-to-end
  (profile → model → generate → verify → extend), deploy into microVM
  sandboxes (BoxLite, Microsandbox, or bare), operate as services with
  health checks and monitoring, manage lifecycle (upgrade, rollback, backup,
  scale), integrate via OpenAPI/MCP/REST, and troubleshoot every known
  failure mode. Use when the user mentions neuralOS, needle, on-device
  tool-calling, data instances, or wants to build/deploy/manage any
  neuralOS-powered application.
---

# neuralOS — Complete Operations Manual

neuralOS turns any data source into a private, offline question-answering
service. A 121M-parameter model (~35 MB, CPU-only, ~95 MB RAM) reads your
data's actual shape and answers plain-English questions — no cloud, no API
keys, no GPU. This manual covers everything from first install to production
operations on any platform.

## The mental model in one paragraph

neuralOS is a **semantic resolver**, not a chat model. It reads messy human
intent and emits typed, verified queries against your data. Everything around
it — the retrieval front-end, the verification gates, the audit trail, the
health checks — exists to make those queries reliable. The model doesn't
chat; it routes, extracts, and abstains (returns empty when no probe matches).
The system layer catches what the model can't, and the verification gates
prove what it does.

---

## The full stack

```
L5  YOUR PRODUCT      dashboards, chatbots, alerts, compliance reports
L4  INTEGRATION       OpenAPI · MCP · REST · webhooks · A2A mesh
L3  FRAMEWORK         chains · meta-routing · HITL · guardrails
L2  RUNTIME           neuralosd supervisor · engine pool · scheduler ·
                        memory (needle embeddings) · metrics
L1  KERNEL            @probe · router · ask.py · serve.py · verify.py
L0  ENGINE            needle 3 (121M, 2-bit SAN, grammar-caged, ~35 MB)
HOST                BoxLite microVM · Microsandbox · bare metal
```

---

## Phase 0 — Install (all platforms)

### macOS Apple Silicon

```bash
# Python 3.11–3.13 (NOT 3.14 — wheels missing)
python3.12 -m venv ~/neuralos-venv
source ~/neuralos-venv/bin/activate
pip install --upgrade pip
pip install neuralos pydantic

# Optional: BoxLite (microVM hosting)
pip install boxlite

# Optional: Microsandbox (alternative runtime)
curl -fsSL https://install.microsandbox.dev | sh

# Verify
python3 -c "import needle; print('neuralOS', needle.__version__)"
python3 -c "import boxlite; print('boxlite', boxlite.__version__)"
sysctl -n kern.hv_support   # → 1 = Hypervisor.framework OK
```

### Linux x86_64 / ARM64

```bash
# Same Python setup (3.11–3.13)
python3.12 -m venv ~/neuralos-venv
source ~/neuralos-venv/bin/activate
pip install neuralos pydantic

# Verify KVM
[ -e /dev/kvm ] && echo "KVM OK" || echo "Enable KVM in BIOS/kernel modules"
```

### Windows (WSL2)

```powershell
wsl --install
# Inside WSL2:
python3.12 -m venv ~/neuralos-venv
source ~/neuralos-venv/bin/activate
pip install neuralos pydantic
# Verify KVM passthrough
[ -e /dev/kvm ] && echo "KVM OK"
```

### Verify the model works

```bash
python3 -c "
import os
os.environ['NEEDLE_TELEMETRY'] = '0'
os.environ['DO_NOT_TRACK'] = '1'
import needle
print('neuralOS', needle.__version__)
"
```

---

## Phase 1 — PROFILE your data source

The profiler reads the actual data (never guesses) and records every field,
type, range, enum value, and sample.

### Supported source types

| Source kind | `--source` format | Detects |
|---|---|---|
| CSV / TSV | `path/to/file.csv` | columns, types, enums, ranges, samples |
| MySQL / Postgres | `mysql://user:pass@host:port/db` | tables, columns, row counts, enums |
| SQLite | `sqlite:///path/to/db.sqlite` | same as above |
| JSON / JSONL | `path/to/file.jsonl` | nested schemas, field types |
| Log files | `path/to/app.log` | line templates, capture regexes, levels |
| REST API | `https://api.example.com/data` | response shape, auth needed |
| Excel / XLSX | `path/to/file.xlsx` | sheets, columns, values |
| Directories | `path/to/dir/` | per-file-pattern probes |
| Unstructured text | `--source notes.txt --kind text` | extraction schema via model |

### Run the profiler

```bash
# CSV
python3 neuralos/scripts/profile_data.py --source incidents.csv --out profile.json

# MySQL database (usql CLI required)
python3 neuralos/scripts/profile_data.py \
    --source mysql://admin:pass@127.0.0.1:3306/mydb --out profile.json

# JSONL
python3 neuralos/scripts/profile_data.py \
    --source events.jsonl --out profile.json

# Log file
python3 neuralos/scripts/profile_data.py \
    --source /var/log/app.log --out profile.json

# REST API (fetches and profiles the response)
python3 neuralos/scripts/profile_data.py \
    --source https://api.example.com/v1/records --out profile.json
```

### Read the profile before modeling

Open `profile.json` and check:
- **Surprising nullability** — a field that should always exist but has nulls
- **Fields that are really enums** — `status` with only 3 distinct values
- **Dates stored as strings** — needs a datetime validator
- **Values with units or prefixes** — `"NGN46494.89"` needs cleaning
- **Nested structures** — JSON sources may have arrays of objects

---

## Phase 2 — MODEL (strict Pydantic)

```bash
python3 neuralos/scripts/gen_pydantic.py \
    --profile profile.json --out models.py \
    [--table table_name]        # for database sources
```

The generated `models.py` contains strict Pydantic v2 models:
- Types inferred from the data
- Constraints (`ge`, `le`, `pattern`, `Literal`/`Enum`) from observed ranges
- `Optional` where nulls occur
- Datetime fields with multi-format parsing
- `extra="forbid"` — rejects unexpected keys
- Aliases matching the source's own field names
- A description per field with observed statistics

**Edit if the data's meaning demands it.** The generator knows types; only
you know that `status_code = 2` means "In Progress". Add enum labels, rename
cryptic columns, then proceed.

---

## Phase 3 — GENERATE the full instance

```bash
python3 neuralos/scripts/gen_needle_instance.py \
    --profile profile.json \
    --models models.py \
    --out my_instance \
    [--db-dsn "mysql://user:pass@host/db"] \
    [--runtime python|engine] \
    [--agent-name myfeed] \
    [--table table_name]
```

This generates the **complete instance directory**:

| File | Role |
|---|---|
| `needle_menu.json` | probe menu with triggers + grammar-caged arguments |
| `bridge.py` | reads the source, validates through models, returns small results |
| `instance.py` | the agentic agent (deprecated CLI — use ask.py) |
| `ask.py` | **mandated entry point**: lexical top-K retrieval + deterministic fast path + stale-results guard + audit + cache + PII mask |
| `serve.py` | standard service: `/healthz` (liveness), `/ready` (data check), `POST /ask` |
| `golden.json` | golden question bank — one per probe |
| `CATALOG.md` | human "what you can ask" reference |
| `invariants.py` | property checks (counts, non-negative money) |
| `truth.json` | per-table SQL oracle cross-checks (database sources) |
| `openapi.json` | OpenAPI 3.1 typed operations |
| `mcp.json` | MCP tool manifest for AI agent hosts |
| `graph_edges.json` | relationship layer (if discovered) |
| `graph_bridge.py` | runtime graph traversal |
| `verify.py` | verification runner (all gates) |
| `README.md` | instance-specific documentation |

### Source-specific generation

The generator auto-detects the source kind and generates the right bridge:

| Source kind | Bridge behavior | Probes generated |
|---|---|---|
| Database | SQL via usql/bridge, parameterized | count/query/aggregate per table |
| CSV/TSV | direct read, dialect sniffed | peek/filter/count/aggregate |
| JSON/JSONL | flatten nested, direct read | peek/filter/count |
| Log files | line templates + capture regexes | parse_tail/count_by_level/search |
| REST API | HTTP fetch with baked auth | per-resource probes |
| Excel | openpyxl if available | per-sheet peek |
| Unstructured text | model extraction with schema | extract(record_text) |

---

## Phase 4 — VERIFY (never skip)

```bash
cd my_instance

# Model coverage (must be ≥ 95%)
python3 verify.py

# Selection test (3 phrasings per probe)
python3 verify.py --full

# Truth check (probe vs direct source query)
python3 verify.py --truth

# Golden question bank (all seeded questions)
python3 verify.py --golden

# Property invariants
python3 verify.py --invariants

# Everything
python3 verify.py --full --truth --golden --invariants
```

**The gates:**

| Gate | What it proves | Pass threshold |
|---|---|---|
| Model coverage | % of records that parse through the model | ≥ 95% |
| Selection test | Correct probe picked for each phrasing | 3/3 per probe |
| Truth check | Relayed number == direct source query | 100% match |
| Invariants | Property checks (counts, sums, ranges) | all pass |
| Golden bank | Every seeded question produces results | 100% |

---

## Phase 5 — EXTEND (the improvement loop)

When the instance can't answer a question, it **refuses honestly** and logs
the gap. Mine the gaps to grow the instance:

```bash
# mine gated/fuzzy questions from the audit log
python3 neuralos/scripts/mine_gaps.py ask_audit.jsonl \
    --menu my_instance/needle_menu.json

# lint triggers for collisions (before AND after menu changes)
python3 neuralos/scripts/lint_triggers.py my_instance/needle_menu.json

# calibrate per-probe confidence gates from the golden bank
python3 neuralos/scripts/calibrate.py \
    --golden my_instance/golden.json \
    --base http://localhost:8877
```

The improvement loop:
1. **Mine** — collect refused/fuzzy questions from the audit log
2. **Propose** — mine_gaps suggests new triggers for existing probes or
   candidates for new probes
3. **Apply** — add triggers/probes to the menu (human review)
4. **Lint** — check for trigger collisions
5. **Re-verify** — full suite (golden + truth + selection)

---

## Deploy into microVM sandboxes

See the `boxlite-neuralos` and `msb-neuralos` skills for the full deployment
patterns. The short version:

### BoxLite (recommended for services)

```bash
# Cook a template with neuralOS preinstalled
venv/bin/python - <<'PY'
import boxlite
rt = boxlite.Boxlite.default()
rt.create(boxlite.BoxOptions(
    image="python:3.12-slim", auto_delete=0,
    cpus=1, memory_mib=1024, disk_size_gb=6,
    name="neuralos-template"))
PY
boxlite exec neuralos-template -- pip install neuralos pydantic pymysql
boxlite stop neuralos-template   # disk persists

# Clone for each deployment
boxlite new my-app --fork neuralos-template
```

### Microsandbox (fork a running machine)

```bash
msb create --name neuralos-tpl python:3.12-slim
msb exec neuralos-tpl -- pip install neuralos pydantic
msb snapshot create --sandbox neuralos-tpl --full -o template.msb
msb snapshot restore template.msb --name my-app --forked
```

---

## Operating a deployed instance

### Start / stop

```bash
# BoxLite
boxlite start my-instance
boxlite stop my-instance        # processes die, disk persists

# Inside the box (relaunch after start)
boxlite exec my-instance -- python3 /opt/instance/serve.py --port 8877 &
```

### Ask questions

```bash
# CLI (one-shot)
boxlite exec my-instance -- python3 /opt/instance/ask.py "how many records"

# HTTP (resident daemon)
curl -X POST http://localhost:8877/ask \
    -H "Content-Type: application/json" \
    -d '{"question": "how many records"}'
```

### Monitor

```bash
boxlite exec my-instance -- cat /opt/instance/ask_audit.jsonl | tail -5
boxlite exec my-instance -- python3 -c "
import json
for line in open('/opt/instance/ask_audit.jsonl'):
    r = json.loads(line)
    print(r.get('probe'), r.get('confidence'), r.get('latency_ms'))
"
```

### Backup

```bash
# Export the box as a portable archive
venv/bin/python -c "
import boxlite, asyncio
async def main():
    rt = boxlite.Boxlite.default()
    box = await rt.get('my-instance')
    await box.export(dest='backup.msb')
asyncio.run(main())
"
```

---

## Sizing guide (measured)

| Instance type | vCPU | RAM | Disk | Observed |
|---|---|---|---|---|
| neuralOS only | 1 | 1 GB | 4 GB | idle 178 MB, peak 357 MB, model 95 MB |
| + MariaDB data source | 1 | 1 GB | 6 GB | +150 MB for MariaDB, disk +157 MB |
| Multi-tenant (10 sandboxes) | 2 | 4 GB | 20 GB | shared engine, per-box isolation |

---

## Failure modes and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'needle'` | neuralOS not installed in the venv | `pip install neuralos` in the correct venv |
| `cannot import name 'Record' from 'models'` | models.py missing or empty | Re-run `gen_pydantic.py` with the correct profile |
| Probe returns `{"error": "..."}` | Data source unreachable | Check connection (DB URL, file path, API auth) |
| Probe returns empty results | No probe matched the question | Check triggers in the menu; add missing phrasings |
| Answers seem stale (previous question's data) | Results gate missing | Use the generated `ask.py` (has the guard built in) |
| `sqlite3: 14 unable to open` | MySQL dump imported into MariaDB | Re-dump with `--set-gtid-purged=OFF` and sed collation |
| `ERROR 1044 access denied` on import | MariaDB admin grants incomplete | Grant for BOTH `%` and `localhost` |
| Trigger collisions in lint | Overlapping triggers between probes | Reword, remove, or add distinguishing tokens |
| Box won't start after recreate | Rootfs wiped | Self-installing entrypoint or snapshot restore |
| Model gives wrong answers after menu edit | Selection degraded | Re-run `lint_triggers.py`; re-run full selection suite |
| Python 3.14 wheel install fails | Wheels not built for 3.14 | Use Python 3.11–3.13 |

---

## Complete file reference

Every instance directory contains:

```
my_instance/
├── needle_menu.json       # probe menu: triggers + grammar-caged args
├── bridge.py              # data layer: reads source, validates via models
├── models.py              # strict Pydantic models (from profiling)
├── instance.py            # agentic agent (deprecated CLI — use ask.py)
├── ask.py                 # MANDATED entry point (retrieval + fast path)
├── serve.py               # standard web service (/healthz /ready /ask)
├── golden.json            # golden question bank
├── truth.json             # SQL cross-checks (database sources)
├── invariants.py          # property checks
├── CATALOG.md             # human "what you can ask"
├── openapi.json           # OpenAPI 3.1 typed operations
├── mcp.json               # MCP tool manifest
├── graph_edges.json       # relationship layer
├── graph_bridge.py        # runtime graph traversal
├── verify.py              # verification runner
└── verification.txt       # phase-4 evidence
```
