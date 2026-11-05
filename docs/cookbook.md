# neuralOS Agentic Cookbook

Recipes for building real applications with neuralOS. Each recipe is
complete — copy, paste, modify, run. Written for coding agents to follow
step by step without human intervention.

---

## Recipe: Production deployment checklist

Take an instance from "works on my machine" to "trusted by a department":

```bash
# 1. verify the environment
neuralosd doctor --instance-dir ./mydata

# 2. lock down access
neuralosd roles --instance-dir ./mydata --op set --role viewer \
    --allow row_count,breakdown --row-filter "region=in:North"
neuralosd serve --instance-dir ./mydata --auth-token $TOKEN

# 3. prove the answer quality
neuralosd golden --dir ./mydata --record --question "total revenue" \
    --expect-probe total_revenue --value 12642501.91 \
    --by olu --source "global_superstore sum" --method "SQL cross-check"
neuralosd badge --instance-dir ./mydata --out badge.json \
    --actions-out .github/workflows/golden.yml

# 4. know when users are asking for more
neuralosd slo --instance-dir ./mydata --max-refusal-rate 0.4
neuralosd gaps ./mydata/ask_audit.jsonl --out gaps.json

# 5. keep the menu tight as usage evolves
neuralosd mothball --instance-dir ./mydata --age-days 90 --apply

# 6. audit schema changes before they break probes
neuralosd drift --instance-dir ./mydata --source ./fresh_dump.csv

# 7. stream live logs into the instance
neuralosd watch --instance-dir ./logs \
    --source syslog:/var/log/syslog --rules rules.json --interval 30
```

## Recipe: Close the loop — from dissatisfaction to capability

The moat grows from the highest-signal data there is: what users asked for
and did not get.

```bash
# 1. see the PATTERNS, not anecdotes:
neuralosd gaps ask_audit.jsonl --out gaps.json
#    -> clusters: "this gap cost you 37 refused queries across 12 phrasings"

# 2. compare notes with the mesh (anonymized fingerprints, never questions):
neuralosd gaps ask_audit.jsonl --export-fingerprints fp.json
neuralosd gaps ask_audit.jsonl --ingest-fingerprints peer_fp.json
#    -> "peers saw this pattern 300 more times — 337 asks total"

# 3. a user thumbs-down becomes a candidate, with the correction attached:
neuralosd feedback --instance-dir . --question "shipping cost per unit" \
    --verdict bad --correction "average shipping cost per unit" --by ada
neuralosd feedback --instance-dir . --apply

# 4. build the probe (reason loop, two models, oracle-checked):
neuralosd reason --instance-dir . "average shipping cost per unit" \
    --oracle 12.42 --consensus-model qwen3:8b --shadow

# 5. prove it agrees with its oracle, then promote:
neuralosd shadow --instance-dir . --evaluate
neuralosd shadow --instance-dir . --promote

# 6. the misuse trap was synthesized and refusal-proven at birth; the
#    golden case carries its receipt:
neuralosd golden --dir . --receipts

# 7. keep the menu tight:
neuralosd mothball --instance-dir . --age-days 180 --apply
```

And when the source changes under you:

```bash
neuralosd drift --instance-dir . --source ./fresh_dump.csv   # exit 1 = re-audit
```

When a sibling estate shares the schema shape, transfer what you proved:

```bash
neuralosd transfer --from ~/instances/finacle_a --to ~/instances/finacle_b
```

## Recipe: SQLite database → instance in 60 seconds

`neuralosd init` reads SQLite files directly with the Python standard
library — no drivers, no server, works in the frozen binary:

```bash
neuralosd init --source ledger.db --name ledger --out ./ledger-inst
neuralosd ask --instance-dir ./ledger-inst "how many rows"
neuralosd ask --instance-dir ./ledger-inst "total amount by region"
```

What you get:

- **Every table profiled** — columns, declared types, row counts; all tables
  are named in the generated `probes.py` so the menu documents the schema.
- **The first table drives the probes** — the same scaffold a single-sheet
  workbook gets: `row_count`, `list_rows`, `distinct_<col>`,
  `total_<col>` / `average_<col>`, and the caged `breakdown` probe.
- **Auto-trap bank** — every probe ships with its misuse trap in
  `traps.json` from birth (see the 2.1.0 changelog), so the refusal
  contract is regression-tested from the first ask.

Works for app databases, downloaded `.sqlite` exports, and WAL-mode files
(opened read-only — init never writes to your data).

---

## Recipe: Audit an Excel workbook (formulas, sheets, named ranges)

The deep Excel connector does not stop at reading rows:

```bash
neuralosd init --source finance_model.xlsx --name model --out ./model-inst
neuralosd ask --instance-dir ./model-inst "list sheets"
neuralosd ask --instance-dir ./model-inst "named ranges"
neuralosd ask --instance-dir ./model-inst "formula scan"
neuralosd ask --instance-dir ./model-inst "formulas in assumptions"
```

- **`sheets`** — every sheet with row, column and formula counts.
- **`named_ranges`** — every defined name with its target and a bounded
  value preview (a range that resolves to nothing reports the error on the
  entry — that is a finding about the workbook, not a silence).
- **`formula_scan`** — count + samples of formula cells, caged to the
  workbook's own sheet names ("formulas in <sheet>").
- The header row is data-free by definition: row_count counts DATA rows,
  exactly like the CSV path.

## Recipe: Point an instance at your logs

Three log sources normalize to the same event shape
(`{ts, host, app, level, message}`), so the standard probes
(`row_count`, `distinct_app`, `distinct_level`, …) just work:

```bash
# syslog text file (stdlib only — works in the frozen binary)
neuralosd init --source syslog:/var/log/system.log --name syslog

# journald (bounded, newest first; optional unit filter)
neuralosd init --source journal: --name journal
neuralosd init --source journal:nginx.service --name nginx

# Windows Event Log (run on the Windows host that owns the channel)
neuralosd init --source wevtutil:System --name sysmon
```

Honesty rules the log connector follows:

- **Bounded**: reads are newest-first up to `NEURALOSD_LOG_CAP`
  (default 10000) — a snapshot, and the cap is recorded in the bridge.
- **Holes are visible**: a syslog line that does not parse is counted in
  `LOG_INTEGRITY.parse_errors` (embedded in the generated bridge), never
  silently dropped.
- **Right host**: `journalctl`/`wevtutil` must run on the machine that owns
  the log — point the instance there (or deploy the instance to it).

## Recipe: Group-by / breakdowns

Any instance built by `neuralosd init` already has a `breakdown` probe:

```bash
neuralosd init --source xlsx/global_superstore_2016.xlsx --name store
neuralosd ask --instance-dir ./store "revenue breakdown by year"
neuralosd ask --instance-dir ./store "sales by market"
neuralosd ask --instance-dir ./store "profit by category"
```

It sums a **measure** per **dimension**:

```
{ "by": "year", "measure": "Sales", "groups": 4,
  "rows": [ {"year": "2012", "Sales": 2259450.9},
            {"year": "2013", "Sales": 2677438.69}, ... ],
  "total": 12642501.91 }
```

**Dimensions** — low-cardinality categorical columns, plus `year` / `quarter`
/ `month` derived from a detected date column.

**Measures** — numeric columns that are not identifiers. The measure is
optional and defaults to the first one, so `"breakdown by year"` works even
if you never name a column.

Add your own grouped probe when you need a shape the generator does not
produce (a ratio, a top-N, a filter):

```python
@probe(description="Top regions by profit",
       triggers=["top regions", "best regions by profit"])
def top_regions():
    rows = bridge.breakdown("Region", "Profit")["rows"]
    top = sorted(rows, key=lambda r: -r["Profit"])[:5]
    return {"top": top}
```

The parts always reconcile with the flat total —
`breakdown(d, m)["total"] == total(m)["sum"]`.

## Recipe: Use a library the binary doesn't have

A frozen binary cannot import your machine's packages. When a probe needs one
it lacks, it runs that probe in the host Python via the **sidecar**.

**1. Write the probe normally** — import the library inside the function:

```python
from neuralosd import probe

@probe(description="Count pages in a PDF", triggers=["pdf pages", "pdf"])
def pdf_pages(path):
    from pypdf import PdfReader       # resolved in the sidecar
    return {"pages": len(PdfReader(path).pages)}
```

**2. Give the sidecar the library** — either of:

```bash
pip install neuralosd pypdf            # machine already has Python
neuralosd sidecar --setup --with pypdf # let uv build the environment
```

**3. Use the binary as normal** — delegation is automatic:

```bash
./neuralosd-macos-arm64 ask --instance-dir ./pdfs "pdf pages"
```

**What happens if you skip step 2?** A clear, non-crashing exit:

```
error: probe 'pdf_pages' needs the Python module 'pypdf',
       which is not available in this environment.
```

**Notes**

- The sidecar starts lazily — only for calls that need it. Everything else
  stays in-process and fast.
- `NEURALOSD_SIDECAR=off` disables delegation; `NEURALOSD_SIDECAR=/path/to/helper`
  points at a specific helper.
- To force delegation regardless of what the binary carries, declare the probe
  with `tier="sidecar"`.
- The binary never needs rebuilding to gain a library.

## Recipe 1: Analyst-in-a-Box (any CSV)

**Goal**: Turn any CSV file into an offline question-answering service
inside an isolated microVM.

### What you need
- A CSV file
- Python 3.12 venv with `neuralos` installed
- BoxLite (or Microsandbox)

### Steps

```python
# ============================================================
# STEP 1: Profile the CSV
# ============================================================
import subprocess, sys

result = subprocess.run([
    sys.executable, "neuralos/scripts/profile_data.py",
    "--source", "your_data.csv",
    "--out", "profile.json"
], capture_output=True, text=True)
print(result.stdout[-500:])

# ============================================================
# STEP 2: Generate Pydantic models
# ============================================================
result = subprocess.run([
    sys.executable, "neuralos/scripts/gen_pydantic.py",
    "--profile", "profile.json",
    "--out", "models.py"
], capture_output=True, text=True)
print(result.stdout[-200:])

# ============================================================
# STEP 3: Generate the full instance
# ============================================================
result = subprocess.run([
    sys.executable, "neuralos/scripts/gen_needle_instance.py",
    "--profile", "profile.json",
    "--models", "models.py",
    "--out", "my_analyst",
    "--agent-name", "my_analyst"
], capture_output=True, text=True)
print(result.stdout[-200:])

# ============================================================
# STEP 4: Verify
# ============================================================
os.chdir("my_analyst")
result = subprocess.run([sys.executable, "verify.py", "--full"],
                        capture_output=True, text=True)
print(result.stdout[-500:])

# ============================================================
# STEP 5: Deploy into a BoxLite box
# ============================================================
import boxlite, asyncio

async def deploy():
    rt = boxlite.Boxlite.default()
    got = await rt.get_or_create(
        boxlite.BoxOptions(image="python:3.12-slim", auto_delete=0,
                           cpus=1, memory_mib=1024, disk_size_gb=6),
        name="my-analyst-box")
    box = got[0] if isinstance(got, tuple) else got
    await box.start()
    await box.copy_in("../my_analyst", "/opt/analyst")
    ex = await box.exec("pip", "install", "--no-cache-dir", "neuralos",
                        timeout_secs=600)
    async for line in ex.stdout(): print(line, end="")
    await box.stop()
    print("deployed: my-analyst-box")

asyncio.run(deploy())

# ============================================================
# STEP 6: Ask questions
# ============================================================
import asyncio, boxlite

async def ask(question):
    rt = boxlite.Boxlite.default()
    box = await rt.get("my-analyst-box")
    await box.start()
    ex = await box.exec("python3", ["/opt/analyst/ask.py", question],
                        timeout_secs=120)
    out = []
    async for line in ex.stdout(): out.append(line)
    r = await ex.wait()
    print("".join(out))
    await box.stop()

asyncio.run(ask("how many records are in the dataset"))
```

---

## Recipe 2: Database Analyst (MariaDB/MySQL)

**Goal**: Point neuralOS at a live database and answer questions about it.

```python
# ============================================================
# STEP 1: Profile the database
# ============================================================
result = subprocess.run([
    sys.executable, "neuralos/scripts/profile_data.py",
    "--source", "mysql://user:pass@host:3306/mydb",
    "--out", "profile.json"
], capture_output=True, text=True)

# ============================================================
# STEP 2-3: Generate models + instance (same as Recipe 1)
# ============================================================

# ============================================================
# STEP 4: Verify against the live database
# ============================================================
os.chdir("my_instance")
result = subprocess.run([
    sys.executable, "verify.py", "--full", "--truth"
], capture_output=True, text=True)
print(result.stdout)

# ============================================================
# STEP 5: Ask
# ============================================================
result = subprocess.run([
    sys.executable, "ask.py", "how many customers by country"
], capture_output=True, text=True)
print(result.stdout)
```

---

## Recipe 3: Multi-Instance Router

**Goal**: Route questions to the right instance (finance, ops, HR) using
the meta-selector.

```python
from neuralosd import MetaSelector

# Define instances
selector = MetaSelector({
    "finance": {
        "description": "Financial data: revenue, invoices, customers, payments",
        "examples": ["top customers", "sales by country", "monthly revenue"]
    },
    "ops": {
        "description": "Operations data: incidents, uptime, SLA, alerts",
        "examples": ["open incidents", "P1 escalations", "SLA breaches"]
    },
    "hr": {
        "description": "HR data: employees, departments, salaries, reviews",
        "examples": ["employee count", "salary distribution"]
    },
})

# Route a question
instance = selector.route("how many open P1 incidents")
print(f"Route to: {instance}")  # → "ops"
```

---

## Recipe 4: Probe Chain (multi-step answers)

**Goal**: Answer a compound question that requires multiple probes in
sequence, with results from one step feeding the next.

```python
from neuralosd import ChainRunner

# Register probes
probes_by_name = {p._probe.name: p for p in all_probes}

# Define the chain
runner = ChainRunner(probes_by_name)
runner.register("incident_deep_dive", [
    # Step 0: Get the top incident
    {"probe": "top_incident"},
    # Step 1: Use the incident's team to get team workload
    {"probe": "team_workload",
     "args": {"team": "{{step_0.team}}"}},
    # Step 2: Get similar past incidents for that team
    {"probe": "similar_incidents",
     "args": {"team": "{{step_1.team}}"}},
])

# Execute
result = runner.run("incident_deep_dive")
for step in result["steps"]:
    print(f"Step {step['step']} ({step['probe']}): {step['result']}")
```

---

## Recipe 5: HITL Guardrail (human confirms risky operations)

**Goal**: Require human confirmation before executing destructive probes.

```python
from neuralosd import ConfirmStore

store = ConfirmStore(ttl=300)   # 5-minute expiry

# A risky probe
def delete_record(record_id: int):
    # This would normally delete data
    return {"deleted": record_id}

# In the routing layer:
# If the probe is flagged confirm=True, create a pending confirmation
pending = store.create(delete_record, {"record_id": 42},
                       reason="destructive operation")
print(f"Confirmation required: {pending['confirm_token']}")
print(f"Probe: {pending['probe']}")
# The caller confirms via POST /v1/confirm/{token}

# When confirmed:
result = store.confirm(pending["confirm_token"])
print(f"Executed: {result}")
```

---

## Recipe 6: MCP Server (expose to AI agents)

**Goal**: Make the instance's probes available as MCP tools for Claude,
Cursor, or any MCP host.

```bash
# Start the MCP server (reads JSON-RPC from stdin, writes to stdout)
python3 -m neuralosd.mcp /path/to/instance

# Or add to Claude Desktop's MCP config:
# {"mcpServers": {"my-analyst": {"command": "python3", "args": ["-m", "neuralosd.mcp", "/path/to/instance"]}}}
```

Test it:
```bash
echo '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}' | \
    python3 -m neuralosd.mcp /path/to/instance
```

---

## Recipe 7: Warm Pool (sub-second responses)

**Goal**: Maintain pre-warmed sandbox clones so answers return in < 1s.

```python
import boxlite, asyncio, time

async def warm_pool(rt, template_name, size=3, prefix="worker"):
    tpl = await rt.get(template_name)
    pool = []
    for i in range(size):
        clone = await tpl.clone_box(name=f"{prefix}-{i}")
        pool.append(clone)
    return pool

async def dispense(pool):
    box = pool.pop(0) if pool else None
    if box:
        await box.start()
    return box

async def recycle(rt, box):
    await box.stop()
    pool.append(box)

# Usage:
async def main():
    rt = boxlite.Boxlite.default()
    pool = await warm_pool(rt, "neuralos-template", size=3)

    # On each request:
    box = await dispense(pool)
    if box:
        ex = await box.exec("python3", ["-c", "import needle; print('ready')"])
        async for line in ex.stdout(): print(line, end="")
        await box.stop()
        # Recycle: clone a replacement
        new_box = await tpl.clone_box(name=f"worker-{len(pool)}")
        pool.append(new_box)
```

---

## Recipe 8: Backup and Restore

**Goal**: Backup an instance and restore it on another host.

```python
# Backup (BoxLite)
import boxlite, asyncio

async def backup(box_name, dest):
    rt = boxlite.Boxlite.default()
    box = await rt.get(box_name)
    await box.export(dest=dest)
    print(f"backed up to {dest}")

asyncio.run(backup("my-analyst-box", "~/backups/my-analyst.boxlite"))

# Restore on another host
import boxlite, asyncio

async def restore(archive, name):
    rt = boxlite.Boxlite.default()
    box = await rt.import_box(archive, name=name)
    print(f"restored: {name}")

asyncio.run(restore("~/backups/my-analyst.boxlite", "my-analyst"))
```

---

## Recipe 9: Upgrade (rolling, with rollback)

**Goal**: Upgrade neuralOS in a running instance without downtime.

```python
# 1. Snapshot the current state (rollback point)
# 2. pip upgrade inside the box
# 3. Re-verify
# 4. If verification fails, roll back to the snapshot

async def upgrade(rt, box_name):
    # Snapshot for rollback
    box = await rt.get(box_name)
    await box.export(dest=f"{box_name}-pre-upgrade.boxlite")

    # Upgrade
    ex = await box.exec("pip", "install", "--upgrade", "--no-cache-dir",
                        "neuralos", timeout_secs=600)
    async for line in ex.stdout(): print(line, end="")

    # Verify
    ex = await box.exec("python3", ["-c", "import needle; print(needle.__version__)"],
                        timeout_secs=60)
    async for line in ex.stdout(): print(line, end="")

    # If verification fails: restore the pre-upgrade export
    # (see Recipe 8 for restore)
```

---

## Recipe 10: Log Monitoring Agent

**Goal**: Use neuralOS to monitor log files and answer questions about them.

```bash
# Profile the log file
python3 neuralos/scripts/profile_data.py \
    --source /var/log/app.log --out log_profile.json

# Generate the instance
python3 neuralos/scripts/gen_pydantic.py \
    --profile log_profile.json --out log_models.py
python3 neuralos/scripts/gen_needle_instance.py \
    --profile log_profile.json --models log_models.py \
    --out log_instance --agent-name log_monitor

# Ask about the logs
cd log_instance
python3 ask.py "how many errors in the last 100 lines"
python3 ask.py "what are the most common log levels"
```

---

## Recipe 11: Trust an answer before you use it

A number you cannot audit is a number you should not report. Every envelope
says what it left out.

```python
from neuralosd._cmd._common import load_instance
from neuralosd.router import NoResults

inst = load_instance("./inst")

try:
    env = inst.ask("revenue breakdown by year")
except NoResults as e:
    env = e.envelope
    print("refused:", env["error"])          # results is None — do NOT report
    raise

# 1. Did anything get dropped?
if env.get("discarded"):
    print("PARTIAL:", env["discarded"])
    # -> {"rows": {"missing_year": 1418}}  the answer is not the answer

# 2. Do the parts reconcile with the whole?
for r in env["results"]:
    if "unaccounted" in r and r["unaccounted"]:
        raise SystemExit(f"breakdown disagrees with the grand total "
                         f"by {r['unaccounted']}")

# 3. Only now is the number reportable
print(env["results"][0]["total"])
```

**Refuse by default in a reporting pipeline.** A partial answer that reaches a
dashboard is worse than an error, because nothing downstream can tell.

```bash
neuralosd ask --instance-dir ./inst --strict "revenue breakdown by year" \
  || echo "not reportable (exit $?)"
```

**In a service**, the ledger travels with the response, so the caller decides:

```python
r = requests.post("http://127.0.0.1:8877/ask",
                  json={"question": "sales by region"}).json()
if r.get("discarded"):
    return {"status": "partial", "ledger": r["discarded"]}
```

### Mine the ledger for menu gaps

A recurring `discarded.terms` value is a filter your menu cannot express. That
is a concrete to-do, not a mystery:

```bash
# which questions were refused, and which filters were dropped
grep -h '"discarded"' inst/ask_audit.jsonl | python3 -c "
import sys, json, collections
c = collections.Counter()
for line in sys.stdin:
    for t in (json.loads(line).get('discarded') or {}).get('terms', []):
        c[t] += 1
for term, n in c.most_common(10):
    print(f'{n:5}  {term}   <- add a probe or a trigger for this')"
```

---

## Recipe 12: Close a gap with a derived metric

A question the menu cannot answer in the right *shape*. Here the router serves
`chinook_top_customers` — a list of customers — for a question asking for a
share. Real data, wrong question.

### 1. Establish the truth yourself, first

Never let the loop be its own oracle. Direct SQL, not through the bridge:

```sql
SELECT SUM(UnitPrice * Quantity) FROM InvoiceLine;                     -- 2328.60
SELECT SUM(spend) FROM (
  SELECT SUM(il.UnitPrice * il.Quantity) AS spend
  FROM InvoiceLine il JOIN Invoice i USING (InvoiceId)
  GROUP BY i.CustomerId ORDER BY spend DESC LIMIT 10);                 -- 451.20
```

so the expected ratio is `451.20 / 2328.60 = 0.193764`.

### 2. Rehearse the loop with nothing at stake

```bash
neuralosd reason --instance-dir ./chinook --oracle 0.193764 --dry-run \
  "what share of total revenue comes from the top 10 customers"
```

Read the trail. The line that matters is the computed value against the oracle:

```
[PASS] matches_outside_oracle: delta 0.00e+00 vs oracle 0.193764
```

### 3. Install

```bash
neuralosd reason --instance-dir ./chinook --oracle 0.193764 \
  "what share of total revenue comes from the top 10 customers"
```

```
  baseline: captured 35 routes (4 of them already refusing)
✓ INSTALLED -> ./chinook/derived.json
  unchanged: 35/35
✓ LOOP CLOSED
```

The target is deterministic now. The model is gone from that path.

### 4. Confirm the model is really gone

```bash
time neuralosd ask --instance-dir ./chinook \
  "what share of total revenue comes from the top 10 customers"
# ~1s, mode=deterministic, percent=19.3764
```

### 5. Leave the model running, not the loop

The success metric for this whole arrangement is that the **model gets called
less over time**. Count it:

```bash
neuralosd ask --instance-dir ./chinook "how many customers" >/dev/null   # no model
grep -c esca /dev/null || true
# the loop only spends a model where the deterministic layer has a hole, and
# every run fills one
```

### What NOT to do

- **Do not skip the oracle.** Without it the check is reported as *unchecked*,
  not as passed. `"verified_against": null` in `provenance` is exactly what it
  sounds like.
- **Do not lower `min_coverage` to make matching easier.** That is the setting
  that stops an installed metric capturing `"total revenue"`.
- **Do not hand-edit `derived.json` to add a quantity id you have not seen a
  probe return.** An id that resolves to nothing fails at ask time.



## Recipe 9: Serve with token auth + SDK client

Put the lock on before the port is reachable.

```bash
export NEURALOSD_API_TOKEN="$(openssl rand -hex 32)"
neuralosd serve --instance-dir ./inst --port 8877 --auth-token "$NEURALOSD_API_TOKEN"
# /healthz and /ready stay open (liveness); everything else needs the token
```

```python
from neuralosd_sdk import NeuralOSDClient, RefusalError

c = NeuralOSDClient("http://127.0.0.1:8877", token=os.environ["NEURALOSD_API_TOKEN"])
try:
    r = c.ask("total revenue by region")
    print(r.probe, r.results)     # verified data
except RefusalError as e:
    print("honest refusal:", e.envelope.get("refusal_reason"))
```

A matching zero-dependency JavaScript client lives at
`clients/javascript/neuralosd-client.mjs` (browsers + Node 18+). Retry
policy in both clients: connection errors and 5xx retry with exponential
backoff; 4xx never — retrying a refusal would be asking for a
hallucination.

## Recipe 10: Fleet import audit as a CI gate

The frozen binary loads instance modules dynamically, so its build cannot
know their imports. The audit closes that gap at birth — and in CI, before
a broken instance ships.

```bash
# locally: every instance under the fleet root
neuralosd audit --root /srv/fleet --host-python /path/to/sidecar/python
```

```yaml
# .github/workflows or any CI step
- name: Instance import audit
  run: |
    neuralosd audit --root ./fleet --json > audit.json
    python -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d['ok'] else 1)"
```

Classification: `bundled` (proven in the runtime), `host-only` (sidecar has
it), `instance-local` (sibling in the instance dir — always resolves),
`missing-everywhere` (instance WILL fail — fix before shipping). Exit code
is non-zero on any missing-everywhere, so the gate just works.


## Recipe 9: Pair with synapse-gateway (cross-org verified answers)

Run the gateway + agentd on the instance host and expose the instance as an
MCP toolbox; mesh peers then delegate data questions and receive verified
answers or typed refusals. Full guide:
[drolu.github.io/synapse-gateway](https://drolu.github.io/synapse-gateway/integrate/).

For cross-org meshes, enable the gateway's payload encryption (1.7.15+):
`-mesh-encryption require` seals cross-org payloads to the receiving edge's
X25519 key — the central broker routes ciphertext only, and the JetStream
mailbox stores ciphertext at rest.
