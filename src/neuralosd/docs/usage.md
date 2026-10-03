# neuralOS Usage & Operational Guide

Step-by-step instructions for installing, configuring, building, deploying,
and operating neuralOS instances. Every command is copy-paste ready.

---

## Table of Contents

1. [Install](#1-install)
2. [Verify the Model](#2-verify-the-model)
3. [Build an Instance](#3-build-an-instance)
4. [Verify the Instance](#4-verify-the-instance)
5. [Deploy into a Sandbox](#5-deploy-into-a-sandbox)
6. [Ask Questions](#6-ask-questions)
7. [Run as a Service](#7-run-as-a-service)
8. [Monitor and Audit](#8-monitor-and-audit)
9. [Manage Lifecycle](#9-manage-lifecycle)
10. [Multi-Instance Operations](#10-multi-instance-operations)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Install

### Option A — single-file binary (no Python needed)

Every tagged release ships standalone Nuitka binaries in three variants. The
Python runtime, the neuralosd framework, the four documentation volumes and
the bundled skills are all embedded in one file.

| Variant | Asset | Adds |
|---|---|---|
| base | `neuralosd-<platform>` | framework + docs + skills + model |
| boxlite | `neuralosd-boxlite-<platform>` | + BoxLite microVM engine |
| msb | `neuralosd-msb-<platform>` | + Microsandbox `msb` runtime |

Platforms: `macos-arm64`, `macos-x64`, `linux-x64`, `linux-arm64`,
`windows-x64` (`.exe`). Verify what a binary carries with `neuralosd backends`.

Availability: base everywhere; boxlite on macOS-arm64 + Linux; msb on
macOS-arm64 + Linux + Windows. (No upstream wheel for the rest — boxlite has
no Windows/Intel-macOS build, microsandbox no Intel-macOS build.)

```bash
# macOS Apple Silicon
curl -L -o neuralosd \
  https://github.com/DrOlu/neuralosd/releases/latest/download/neuralosd-macos-arm64
chmod +x neuralosd && sudo mv neuralosd /usr/local/bin/

neuralosd --help
```

Startup: the **first** run extracts the payload to a cache dir (~10 s on a
cold machine); every later run reuses the cache and starts in ~0.25 s. Set a
custom cache location with `NEURALOSD_CACHE_DIR` if needed.

The binary is self-contained for the **framework** — `init`, `ask`, `serve`,
`lint`, `openapi`, `golden`, `truth`, `invariants`, `calibrate`, `gaps`, `diff`,
`docs`, `backends` all work with no Python installed. Sandbox deployment
(`neuralosd deploy`) still needs BoxLite or the `msb` CLI on the host, exactly
as the pip install does.

### Beyond what's baked in: the sidecar

A frozen binary cannot import the host's packages — installing a library does
not make it visible to the binary. When a probe needs something the binary
does not carry, it runs *that probe* in the host Python instead:

```bash
# on the host:
pip install neuralosd        # provides the neuralosd-sidecar command
pip install pypdf pywinrm    # whatever your probes need

# use the binary normally — delegation is automatic
./neuralosd-msb ask --instance-dir ./mydata "pdf pages"
```

| Situation | Behaviour |
|---|---|
| all of a probe's imports resolve in the binary | runs in-process (fast) |
| an import is missing, even lazily inside the function | retried in the sidecar |
| probe declared `tier="sidecar"` | always runs in the sidecar |
| missing import and no sidecar | exits with `pip install <lib>` |

`NEURALOSD_SIDECAR=off` disables delegation; `NEURALOSD_SIDECAR=/path/to/helper`
points at a specific helper. The binary stays one stable file — you never
rebuild it to gain a library.

#### Provisioning the sidecar (uv)

If the machine has no usable Python, or you would rather not touch its
environment, let uv build a dedicated one:

```bash
neuralosd sidecar --status                       # uv found? provisioned?
neuralosd sidecar --setup --with pypdf,pywinrm   # create it
neuralosd sidecar --setup --python 3.11          # a specific interpreter
neuralosd sidecar --setup --force                # rebuild from scratch
```

Under the hood that is exactly:

```bash
uv python install 3.12                            # non-fatal if it fails
uv venv --python 3.12 ~/.neuralosd/sidecar
uv pip install --python ~/.neuralosd/sidecar/bin/python \
    "neuralosd==<this build>" pypdf pywinrm
```

The environment lives at `~/.neuralosd/sidecar` (override with
`$NEURALOSD_HOME`) and is found automatically — **no environment variables
required**. It is pinned to the same version as the binary, and provisioning
is always explicit because it touches the network.

Requirements: `uv` on PATH (or `$NEURALOSD_UV`).

### What `init` generates

For every data source it produces four kinds of probe:

| Probe | Answers |
|---|---|
| `row_count`, `list_rows` | how many rows; a sample |
| `distinct_<col>` | which values a low-cardinality column has |
| `total_<col>`, `average_<col>` | flat aggregates over numeric columns |
| **`breakdown`** | **group-by: a measure per dimension** |

`breakdown` takes caged arguments, so plain English fills them:

```bash
neuralosd ask --instance-dir ./superstore "revenue breakdown by year"
neuralosd ask --instance-dir ./superstore "sales by market"
neuralosd ask --instance-dir ./superstore "profit by category"
```

```
2012 = 2,259,451   2013 = 2,677,439
2014 = 3,405,746   2015 = 4,299,866      total = 12,642,501
```

Its **dimensions** are the low-cardinality categorical columns plus, when a
date column is detected, the derived parts **year / quarter / month**. Its
**measures** are the numeric columns that are not identifiers — and the
measure is optional, so `"revenue breakdown by year"` works without naming
a column (it uses the first measure, which is why "revenue" resolves to the
`Sales` column).

A note on selection: the router prefers the probe that **accounts for the
most of the question** through its extracted arguments. Without that, "sales
by quarter" matched a flat `total_sales` and returned a single grand total —
a confident answer to a question nobody asked.

### The model fallback (`--model`)

Deterministic routing runs first, always. The model is consulted **only** when
lexical retrieval and argument extraction cannot produce an answer:

```bash
neuralosd ask --instance-dir ./inst "total revenue"          # deterministic
neuralosd ask --instance-dir ./inst --model "what did we earn"   # -> model
```

| Retrieval | With `--model` |
|---|---|
| a probe matches and its args extract | runs deterministically — **the model is never loaded** |
| a probe matches but args are missing | model chooses among the retrieved candidates |
| nothing matches at all | model is shown the **whole menu** |
| nothing matches, no `--model` | honest refusal: `no probe matched this question` |

The model routes; it does not answer. It may only call the instance's real
probes, and those still execute in the data layer, so results stay verified.

**Thread safety.** The on-device engine is not thread-safe — building two
engines on two threads **segfaults the process** (reproduced with six parallel
`/ask` calls against `serve --model`). All model work therefore runs behind a
single lock, so model questions are serialised. The deterministic path is
unaffected and stays fully concurrent.

### Keeping up with the components

neuralosd is a composition — the model runtime (`neuralos`), two sandbox
backends (`boxlite`, `microsandbox`) and the data toolchain (`pydantic`, `pymysql`, `openpyxl`). When any of them ships a
release, there is an automated path to follow.

The check ships as a script, so you can run it yourself:

```bash
python scripts/component_versions.py --check      # report drift vs the snapshot
python scripts/component_versions.py --check --record   # update the snapshot
python scripts/component_versions.py --bump patch      # bump neuralosd
```

Drift is detected by diffing live PyPI against `components.json`, which is
committed — so the state is auditable and a re-run with no changes is a no-op.
Each watched component declares a **scope**:

| Scope | A new release triggers |
|---|---|
| `runtime` | a new PyPI release **and** rebuilt binaries |
| `build` | rebuilt binaries only (e.g. a compiler update) |

Pre-releases are skipped. An unreachable PyPI is reported as `unknown` and is
**never** treated as drift — an outage must not mass-bump a release.

In CI this runs daily (`.github/workflows/auto-update.yml`), and the gate is
real: it tests with the current components, upgrades, then tests **again
against the new ones** before anything is published.

### Option B — pip (adds the model + sandbox backends)

### What you need

| Requirement | Check |
|---|---|
| macOS Apple Silicon, Linux (KVM), or Windows/WSL2 | See below |
| Python 3.10–3.13 (not 3.14) | `python3.12 --version` |
| ~2 GB disk for packages + sandbox state | `df -h ~` |

### macOS Apple Silicon

```bash
# Check virtualization support
sysctl -n kern.hv_support
# Output must be: 1

# Create a virtual environment
python3.12 -m venv ~/neuralos-venv
source ~/neuralos-venv/bin/activate

# Install neuralOS runtime + framework
pip install --upgrade pip
pip install neuralos pydantic
```

### Linux x86_64 / ARM64

```bash
# Check KVM support
ls -la /dev/kvm
# If missing, enable:
sudo modprobe kvm_intel    # Intel
sudo modprobe kvm_amd      # AMD

# Create venv and install
python3.12 -m venv ~/neuralos-venv
source ~/neuralos-venv/bin/activate
pip install --upgrade pip
pip install neuralos pydantic
```

### Windows (WSL2)

```powershell
wsl --install
# Inside WSL2:
python3.12 -m venv ~/neuralos-venv
source ~/neuralos-venv/bin/activate
pip install neuralos pydantic
```

### Optional: Install sandbox runtimes

```bash
# BoxLite (Python SDK, recommended for services)
pip install boxlite

# Microsandbox (CLI + runtime)
curl -fsSL https://install.microsandbox.dev | sh
```

### Verify everything works

```bash
python3 -c "import needle; print('neuralOS', needle.__version__)"
python3 -c "import pydantic; print('pydantic', pydantic.__version__)"
python3 -c "import boxlite; print('boxlite', boxlite.__version__)"  # if installed
```

---

## 2. Verify the Model

Before building anything, confirm the 121M model loads and runs:

```bash
python3 -c "
import os
os.environ['NEEDLE_TELEMETRY'] = '0'
os.environ['DO_NOT_TRACK'] = '1'
import needle

@needle.tool(triggers=['add two numbers', 'sum'])
def add(a: int, b: int) -> int:
    'Add two numbers.'
    return a + b

agent = needle.Needle(tools=[add])
r = agent.run('add 4 and 5')
print('model OK, result:', r.get('results'))
"
# Expected output: model OK, result: [9]
```

If this fails, check:
- Python version (must be 3.10–3.13)
- The `neuralos` pip package is installed in the active venv
- No firewall blocking local execution (the model runs entirely in-process)

---

## 3. Build an Instance

An "instance" is a named set of probes that answer questions about a
specific data source. You build one by profiling your data, generating
the models and probes, and verifying the results.

### 3.1 Profile your data

The profiler reads your actual data and records every field, type, range,
enum value, and sample. It never guesses.

```bash
# CSV file
python3 neuralos/scripts/profile_data.py \
    --source ./data/records.csv \
    --out ./profile.json

# MySQL database (requires usql CLI)
python3 neuralos/scripts/profile_data.py \
    --source "mysql://user:pass@host:3306/mydb" \
    --out ./profile.json

# SQLite database
python3 neuralos/scripts/profile_data.py \
    --source "sqlite:///path/to/data.db" \
    --out ./profile.json

# JSON file
python3 neuralos/scripts/profile_data.py \
    --source ./data/events.jsonl \
    --out ./profile.json

# Log file
python3 neuralos/scripts/profile_data.py \
    --source /var/log/app.log \
    --out ./profile.json

# REST API (fetches and profiles the response)
python3 neuralos/scripts/profile_data.py \
    --source https://api.example.com/v1/data \
    --out profile.json
```

### 3.2 Read the profile

```bash
python3 -c "
import json
p = json.load(open('profile.json'))
for t in p.get('source', {}).get('tables', []):
    print(f\"table: {t['name']} ({t.get('rows')} rows)\")
    for f in t.get('fields', []):
        print(f\"  {f['name']}: {f.get('detected_type')} \" +
              f\"(nulls: {f.get('nulls', 0)}, distinct: {f.get('distinct', '?')})\")
"
```

**Check before proceeding:**
- Fields that should never be null but are
- Fields that should be enums but are stored as strings
- Dates stored as text
- Values with units embedded (`"NGN46494.89"`)

### 3.3 Generate Pydantic models

```bash
python3 neuralos/scripts/gen_pydantic.py \
    --profile profile.json \
    --out models.py \
    [--table table_name]
```

**Edit if needed:** the generator knows types, but only you know that
`status = 2` means "In Progress". Add enum labels, rename cryptic columns.

### 3.4 Generate the full instance

```bash
python3 neuralos/scripts/gen_needle_instance.py \
    --profile profile.json \
    --models models.py \
    --out ./my_instance \
    [--db-dsn "mysql://user:pass@host/db"] \
    [--runtime python] \
    [--agent-name myfeed] \
    [--table table_name]
```

This generates the complete instance directory:

```
my_instance/
├── needle_menu.json       # probe menu (triggers + grammar-caged args)
├── bridge.py              # data layer
├── models.py              # strict Pydantic models
├── instance.py            # agentic agent
├── ask.py                 # MANDATED entry point
├── serve.py               # standard web service
├── golden.json            # golden question bank
├── truth.json             # SQL cross-checks (database sources)
├── invariants.py          # property checks
├── CATALOG.md             # human "what you can ask"
├── openapi.json           # OpenAPI 3.1 spec
├── mcp.json               # MCP tool manifest
├── graph_edges.json       # relationships
├── graph_bridge.py        # graph traversal
├── verify.py              # verification runner
└── verification.txt       # phase-4 evidence
```

---

## 4. Verify the Instance

**Never skip verification.** Every gate must pass before you trust the
instance's answers.

```bash
cd my_instance

# Model coverage (≥ 95% required)
python3 verify.py

# Selection test (3 phrasings per probe)
python3 verify.py --full

# Truth check (probe vs direct source query)
python3 verify.py --truth

# Golden question bank
python3 verify.py --golden

# Property invariants
python3 verify.py --invariants

# Everything at once
python3 verify.py --full --truth --golden --invariants
```

**If any gate fails:**
- Coverage < 95% → the model is too strict; widen constraints or clean data
- Selection test fails → check triggers; run `lint_triggers.py`
- Truth check fails → the bridge is reading the wrong data
- Invariants fail → fix the data or the probe logic

---

## 5. Deploy into a Sandbox

### 5.1 BoxLite (recommended for services)

```python
import boxlite, asyncio

async def main():
    rt = boxlite.Boxlite.default()

    # Create a persistent box
    got = await rt.get_or_create(
        boxlite.BoxOptions(
            image="python:3.12-slim",
            auto_delete=0,          # disk survives stop/start
            cpus=1, memory_mib=1024,
            disk_size_gb=6,
        ),
        name="my-instance-box")
    box = got[0] if isinstance(got, tuple) else got
    await box.start()

    # Install dependencies
    ex = await box.exec("pip", "install", "--no-cache-dir",
                        "neuralos", "pydantic", "pymysql",
                        timeout_secs=600)
    async for line in ex.stdout(): print(line, end="")

    # Copy instance files
    await box.copy_in("./my_instance", "/opt/instance")

    # Verify inside the box
    ex = await box.exec("python3", ["-c", "import needle; print('OK')"])
    async for line in ex.stdout(): print(line, end="")

    await box.stop()   # disk persists

asyncio.run(main())
```

### 5.2 Microsandbox

```bash
msb create --name my-instance -c 1 -m 1G python:3.12-slim
msb exec my-instance -- pip install neuralos pydantic
msb copy ./my_instance my-instance:/opt/instance
```

### 5.3 Clone for scale

```bash
# BoxLite: clone the template (CoW, near-instant)
venv/bin/python -c "
import boxlite, asyncio
async def main():
    rt = boxlite.Boxlite.default()
    tpl = await rt.get('my-instance-box')
    clone = await tpl.clone_box(name='my-instance-copy')
    print('cloned:', clone.id)
asyncio.run(main())
"

# Microsandbox: fork the running sandbox (RAM-accurate)
msb snapshot create --sandbox my-instance --full -o template.msb
msb snapshot restore template.msb --name my-instance-copy --forked
```

**⚠ Never clone a sandbox that has unique persisted identity** (API keys,
mesh keypairs). Only fork stateless instances.

---

## 6. Ask Questions

### CLI (one-shot)

```bash
cd my_instance
python3 ask.py "how many records are in the database"
```

### Python (programmatic)

```python
import os, sys
os.chdir("/path/to/my_instance")
sys.path.insert(0, ".")

from ask import ask_one
env = ask_one("how many records are in the database")
print(env["results"])
```

### HTTP (service mode)

```bash
# Start the service
python3 serve.py --port 8877 &

# Ask via HTTP
curl -X POST http://localhost:8877/ask \
    -H "Content-Type: application/json" \
    -d '{"question": "how many records"}'
```

### What a response looks like

```json
{
  "ask_id": "a140e8748ba5",
  "question": "how many records",
  "normalized": "how many records",
  "probe": "count_records",
  "menu_version": "92bfd18ccd6c",
  "mode": "retrieval",
  "confidence": 0.3956,
  "latency_ms": 97,
  "results": [{"count": 3503}]
}
```

### Trusting an answer: the `discarded` ledger

An answer is only an answer if it is an answer to **the question you asked**. A
router that quietly drops a filter, or a probe that quietly drops rows,
produces a number that looks perfectly reasonable and is wrong.

Every envelope therefore carries a `discarded` block whenever something was
left out:

```json
{
  "probe": "breakdown",
  "results": [{"by": "year", "total": 300.0,
               "grand_total": 600.0, "unaccounted": 300.0,
               "rows_in": 3, "rows_counted": 2,
               "skipped": {"missing_year": 1},
               "rows": [{"year": "2012", "Sales": 300.0}]}],
  "discarded": {"rows": {"missing_year": 1}}
}
```

Two vectors, one ledger:

| Key | Meaning |
|---|---|
| `discarded.terms` | a filter value the question named that no argument consumed |
| `discarded.rows` | rows the probe excluded from its own computation |

A non-empty ledger means the answer is **not** an answer to your question.
Nothing is cached when the ledger is non-empty, because memoizing a partial
answer as if it were complete makes it permanent for the whole TTL.

**Probes self-account.** Every generated aggregate reports `rows_in`,
`rows_counted` and `skipped`, and `breakdown` also reports `grand_total` and
`unaccounted` — the difference between the sum of its groups and the total over
all rows. That difference used to be invisible, and it is exactly how a
breakdown can add up internally while disagreeing with the grand total.

Use `--strict` to refuse instead of returning a partial answer:

```bash
neuralosd ask --instance-dir ./inst --strict "sales by year"
# exit 2, error: would have ignored row(s) missing_year=1 — refusing in strict mode
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | answered — `results` is populated |
| `2` | refused — `results` is `null`, `error` explains why |

A refusal prints a JSON envelope, so check the exit code (or `results`) to tell
an answer from a refusal. In Python, a refusal raises `NoResults`.

### Arguments are never coerced

An out-of-range argument is refused, not clamped. `user 999` against an
argument declared `1..10` used to return the record for `user 10` — a
**different** record, silently. Now:

```
user=999 is outside the supported range (1..10) — widen the argument's min/max
```

### What reaches the disk

`--pii-mask off` (or `pii_mask=False`) governs the **response**, not storage. The
cache expires; `ask_audit.jsonl` is append-only and permanent. Secrets and PII
are therefore masked on write regardless of that setting, by key name **and** by
value shape — a JWT under a key called `notes` is still a JWT.

```bash
neuralosd scrub --instance-dir ./inst --dry-run   # what is on disk today?
neuralosd scrub --instance-dir ./inst             # rewrite it masked
neuralosd scrub --instance-dir ./inst --purge-cache
```

`scrub` is the remediation path for records written before a fix; it reports
which secret shapes it found without printing them.

---

### When nothing can answer: the reasoning loop

Some questions are the wrong SHAPE for the menu, not merely missing from it:

```
"what is the average shipping cost per unit"   -> shipped cost per line item (3.5x off)
"what share of revenue comes from the top 10"  -> shipped a list of customers, no share
"average number of tracks per album"           -> shipped the album list
```

Real data, wrong question, and it looks fine. These are ratios, and the router
serves the nearest thing it has.

`neuralosd reason` closes that gap — by installing a **derived metric**, which is
data, not code:

```bash
neuralosd reason \
  --instance-dir ~/neuralos-instances/chinook \
  --oracle 0.193764 \
  "what share of total revenue comes from the top 10 customers"
```

The loop, in order:

```
1  a DETERMINISTIC gate decides whether to escalate at all      (no model)
2  every probe is called once -> a CLOSED inventory of quantities
3  the reasoning model picks two ids from that list             (nothing else)
4  ids outside the list are REJECTED; the arithmetic is OURS
5  deterministic invariants check it ("a share cannot exceed 1")
6  an optional oracle checks it against a number the loop never sees
7  every neighbouring question is replayed -> routes must not move
8  a derived.json entry is written
9  the same question is now deterministic and the model is gone
```

**The division of labour is the design.** A 7B model must never judge: in the
run that produced this, it answered *"gap"* and then *"ok"* on identical input.
It must never compute either: it once proposed dividing two probe *names*.

| the model decides | our code decides |
|---|---|
| which two quantities to divide | whether to escalate |
| | whether the ids exist |
| | the numbers |
| | whether the answer is a valid share |
| | whether anything else moved |

**Nothing a model emits is executed.** The output is two ids from a closed list,
which is why a prompt injection can make it *want* to lie and still cannot
succeed — the id is rejected, and even an accepted pair goes through our
arithmetic.

#### `derived.json`

```json
{"version": 1,
 "metrics": [{"name": "derived_share_total_revenue_comes_from_top",
              "kind": "ratio",
              "numerator": "chinook_top_customers.rows[].total_spend",
              "denominator": "chinook_overview.grand_total_revenue",
              "scale": "percent",
              "triggers": ["what share of total revenue comes from the top 10 customers"],
              "min_coverage": 0.6,
              "provenance": {"model": "deepseek-r1:8b",
                             "verified_against": 0.193764,
                             "checks": ["ids_in_closed_list", "computes",
                                        "expectation_lte_1",
                                        "matches_outside_oracle"]}}]}
```

Every id is a field some probe **really returned** — the inventory is observed,
not declared. `min_coverage` is explained under *Avoiding hijack* below.

#### Avoiding hijack

A new metric must not capture its siblings' questions. In the run that built
this, `"total revenue"` — a 2-token question — started routing to an 8-token
derived trigger, because token-overlap scoring happily reports 2-of-8 the same
as 2-of-2.

`min_coverage` fixes it: the question must contain at least that fraction of the
metric's trigger. `"total revenue"` covers 2/8 = 0.25 and is turned away; the
real question covers 8/8. Only derived metrics opt in, so no existing probe
changes behaviour.

The loop also **replays every trigger in the menu** before and after installing
and refuses to keep a metric that moved anything but its own target.

#### Reading a derived answer

```json
{"probe": "derived_share_total_revenue_comes_from_top",
 "mode": "deterministic",
 "results": [{"percent": 19.3764,
              "numerator":   {"id": "chinook_top_customers.rows[].total_spend", "value": 451.2},
              "denominator": {"id": "chinook_overview.grand_total_revenue", "value": 2328.6},
              "formula": "total_spend / grand_total_revenue",
              "_derived": true}]}
```

The formula travels with the answer, so a reader can check the arithmetic
without trusting the answer.

#### Flags

| Flag | Meaning |
|---|---|
| `--oracle <ratio>` | an independently-computed expected value; the loop never sees where it came from. Without it the check is reported as **unchecked**, not as passed |
| `--model` | Ollama model (default `deepseek-r1:8b`) |
| `--ollama <url>` | default `http://127.0.0.1:11434` |
| `--dry-run` | show the whole trail, write nothing |
| `--force` | escalate even when the gate says not to |
| `--no-stability-check` | ask once instead of twice (faster, less safe) |

```bash
ollama pull deepseek-r1:8b     # the reasoning model lives on-device
```

#### Exit codes

| Code | Meaning |
|---|---|
| 0 | loop closed, or nothing to do |
| 2 | no probe returned anything to build an inventory from |
| 3 | Ollama unreachable, or the model is not pulled |
| 4 | refused — a check failed, or a neighbour moved route |
| 5 | installed but the target question still routes elsewhere |

### Serving a classic needle-menu instance

An instance built before neuralosd carries its declaration in JSON rather than
in `@probe` decorators. `neuralosd` serves it directly:

```bash
neuralosd ask --instance-dir ~/neuralos-instances/chinook "how many customers"
neuralosd serve --instance-dir ~/neuralos-instances/chinook --port 8878
```

The menu's own `parameters` become the caged args, so the same routing and the
same accounting apply. A probe whose module cannot import reports *why*
(`ModuleNotFoundError: No module named 'pymysql'`) rather than "0 routable
probes", which sends you looking in the wrong place.

---

## 7. Run as a Service

### 7.1 Start the standard service

```bash
neuralosd serve --instance-dir ./inst --port 8877
# → http://127.0.0.1:8877
```

`serve` binds **loopback by default**. `/ask` exposes the data behind every
probe and has **no authentication**, so binding wide is a deliberate act:

```bash
neuralosd serve --instance-dir ./inst --host 0.0.0.0 --port 8877
# WARNING: bound to a non-loopback address, and /ask has NO authentication.
# Put an authenticating proxy in front, or bind 127.0.0.1 and tunnel.
```

`--strict` applies the same refusal policy as `ask --strict` to every request.

### 7.2 Endpoints

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/healthz` | GET | — | `{"ok": true}` |
| `/ready` | GET | — | `{"ok": true, "data_layer": "ok"}` |
| `/ask` | POST | `{"question": "…"}` | envelope with results |
| `/openapi.json` | GET | — | OpenAPI 3.1 spec |

### 7.3 Ask via HTTP

```bash
curl -X POST http://localhost:8877/ask \
    -H "Content-Type: application/json" \
    -d '{"question": "top customers"}'
```

### 7.4 Response shape

```json
{
  "ask_id": "a140e8748ba5",
  "question": "top customers",
  "normalized": "top customers",
  "probe": "chinook_top_customers",
  "menu_version": "92bfd18ccd6c",
  "mode": "retrieval",
  "confidence": 0.3956,
  "latency_ms": 97,
  "results": [{"customer": "Helena Holý", "total_spend": 49.62, ...}]
}
```

---

## 8. Monitor and Audit

### Audit log

Every ask is appended to `ask_audit.jsonl`:

```bash
tail -5 ask_audit.jsonl
```

Each line contains:
```json
{
  "ts": 1790817120.678,
  "ask_id": "a140e8748ba5",
  "question": "top customers",
  "normalized": "top customers",
  "probe": "chinook_top_customers",
  "confidence": 0.3956,
  "latency_ms": 97,
  "mode": "retrieval"
}
```

### Gap log

Questions that scored below the confidence gate are logged to
`menu_gaps.jsonl` — these are candidates for new probes:

```bash
tail -3 menu_gaps.jsonl
```

### Lint triggers

After any menu change:

```bash
python3 neuralos/scripts/lint_triggers.py needle_menu.json
```

---

## 9. Manage Lifecycle

### Upgrade the model

```bash
pip install --upgrade neuralos
# Re-run the verification suite
python3 verify.py --full --truth --golden
```

### Upgrade the instance

1. Snapshot the current state (rollback point)
2. Re-profile the data (if the source changed)
3. Re-generate the instance
4. Re-run all verification gates
5. Deploy the new version

### Backup

```bash
# BoxLite: export the box as a portable archive
python3 -c "
import boxlite, asyncio
async def main():
    rt = boxlite.Boxlite.default()
    box = await rt.get('my-instance')
    await box.export(dest='backup.boxlite')
asyncio.run(main())
"

# Microsandbox: full snapshot (disk + RAM + processes)
msb snapshot create --sandbox my-instance --full -o backup.msb
```

### Scale

```bash
# Clone the template for parallel processing
venv/bin/python -c "
import boxlite, asyncio
async def main():
    rt = boxlite.Boxlite.default()
    tpl = await rt.get('my-instance')
    for i in range(5):
        await tpl.clone_box(name=f'worker-{i}')
asyncio.run(main())
"
```

---

## 10. Multi-Instance Operations

When you have multiple instances, the meta-selector routes questions to
the right one:

```python
from neuralosd import MetaSelector

selector = MetaSelector({
    "finance": {
        "description": "revenue, invoices, customers, payments",
        "examples": ["top customers", "sales by country"]
    },
    "ops": {
        "description": "incidents, uptime, alerts, SLA",
        "examples": ["open incidents", "P1 escalations"]
    },
})
instance = selector.route("how many open incidents")
# → "ops"
```

---

## 11. Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'needle'` | neuralOS not installed | `pip install neuralos` |
| `ModuleNotFoundError: No module named 'pydantic'` | Missing dependency | `pip install pydantic` |
| Wrong probe selected | Selector fumbled | Use `ask.py` (has retrieval + fast path) |
| Stale answer from previous question | Results gate missing | Use the generated `ask.py` |
| `NoResults` / empty results | No probe matched | Check the menu; add triggers for the phrasing |
| Import fails inside sandbox | sys.path not set | `sys.path.insert(0, "/opt/instance")` |
| Box won't start | Virtualization not enabled | Enable KVM/HVF/WHP |
| Model slow (> 5s per ask) | Cold start (weights mmap) | Subsequent asks are fast; or use a warm pool |
| Trigger collision detected | Two probes share a trigger | Reword or remove the duplicate |
| PII visible in results | Mask disabled | Ensure `NEURALOS_MASK_PII` is not `0` |

---

## Sizing reference

| Deployment | vCPU | RAM | Disk | Measured |
|---|---|---|---|---|
| neuralOS only | 1 | 1 GB | 4 GB | idle 178 MB, peak 357 MB |
| + MariaDB source | 1 | 1 GB | 6 GB | +150 MB for MariaDB |
| Multi-tenant (10 forks) | 2 | 4 GB | 20 GB | shared base, per-fork delta |
