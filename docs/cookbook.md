# neuralOS Agentic Cookbook

Recipes for building real applications with neuralOS. Each recipe is
complete — copy, paste, modify, run. Written for coding agents to follow
step by step without human intervention.

---

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

