---
name: boxlite-neuralos
description: >-
  End-to-end recipe + runnable scripts for running neuralOS (on-device
  tool-calling model) inside BoxLite microVMs on this host — cook ONE persistent
  template Box with neuralOS preinstalled, verify it, then clone it to spin up
  any number of disposable, isolated job boxes; plus the data-source instance
  variant (e.g. a database-backed agent in its own box with a boot service),
  export/import portability, measured sizing, and every API gotcha met in
  practice. Use when the user mentions BoxLite, boxes, microVM sandboxes,
  cooking/creating a neuralOS template, spinning up neuralOS job boxes,
  cloning boxes, or asks to run neuralOS isolated/sandboxed.
---

# BoxLite ⇄ neuralOS — cook once, clone many

Run the on-device neuralOS model inside hardware-isolated microVMs.
**Cook one persistent template box** with neuralOS preinstalled, then
**clone it** into disposable job boxes in milliseconds. Optionally bake a
data-source instance (database agent) into a box that **auto-loads it at
every boot**.

```
┌──────────────────── host ────────────────────┐
│  your app (Python/Node)                      │
│    └── boxlite SDK  ──┬── template box       │  cook once (persistent)
│                       └── job boxes …       │  clone per task (disposable)
│  Hypervisor.framework / KVM                  │
└──────────────────────────────────────────────┘
```

## Requirements

- **Host**: macOS Apple Silicon (Hypervisor.framework), Linux x86_64/ARM64 (KVM),
  or Windows/WSL2. No root. Check: `sysctl -n kern.hv_support` → `1` (macOS).
- **Python 3.12** for the host-side venv (3.14 is too new for the wheel set —
  3.11/3.12/3.13 work; use `uv venv --python 3.12`).

## Quickstart (end-to-end, ~10 min first time, seconds after)

```bash
SKILL=/Users/olu/.pi/agent/skills/boxlite-neuralos
# 0. one-time environment
bash $SKILL/scripts/env_setup.sh                 # ~/boxlite-lab/venv
PY=~/boxlite-lab/venv/bin/python

# 1. COOK the template (persistent box, neuralOS preinstalled)
$PY $SKILL/scripts/cook_template.py --name neuralos-template

# 2. CLONE it into a job box and ask something
$PY $SKILL/scripts/clone_job.py --template neuralos-template \
    --name job-001 --prompt "what's it like in Lagos right now?"

# 3. (optional) portable archive for another host
$PY $SKILL/scripts/export_box.py --name neuralos-template \
    --dest ~/boxlite-lab/archives/neuralos-template.boxlite
```

---

## How BoxLite actually works (the model in your head)

- A **Box** is a real microVM with its **own kernel** — not a container. Starts
  from any OCI image in **<50 ms** (after first pull), runs on
  Hypervisor.framework (macOS) / KVM (Linux) / WHP (WSL2).
- **Daemonless library**: `pip install boxlite`, embed in your process. Optional
  REST server (`boxlite serve`) for remote/headless.
- **Persistence = disk, NOT processes.** A box's QCOW2 disk survives
  stop/start (with `auto_delete=0`) — installed packages and files stay.
  Running processes do **not** survive; re-exec on start.
- **Boot hook**: BoxLite runs the box's `cmd` as **PID 1 on every start**. This
  is the only "boot script" mechanism — use it.
- Each box is an **isolated network island**: every guest sees the *same*
  eth0 (`192.168.127.2`, gw `.1`, hostname `boxlite`) because each has its own
  user-mode netstack (gvproxy). Identical addresses are safe — boxes cannot see
  each other. Connect boxes **through the host** (published ports / tunnels).
  Never key automation off guest IP or hostname — use **box name/ID**.

---

## Phase 0 — Environment (once)

```bash
bash /Users/olu/.pi/agent/skills/boxlite-neuralos/scripts/env_setup.sh
```

Creates `~/boxlite-lab/venv` (Python 3.12) with `boxlite` + `neuralos`.
All later commands use `~/boxlite-lab/venv/bin/python`.

---

## Phase 1 — COOK the template

`cook_template.py` creates a **persistent** box (`auto_delete=0`), installs
neuralOS (engine + ~35 MB weights bundled in the wheel), runs a **verification
tool call**, and stops it. The disk keeps everything.

```bash
PY=~/boxlite-lab/venv/bin/python
$PY $SKILL/scripts/cook_template.py \
    --name neuralos-template \
    --image python:3.12-slim \
    --cpus 1 --memory-mib 1024 --disk-gb 6 \
    --packages "pymysql pydantic"          # optional extra pip packages
```

What it does, in order (and what you must verify):

| Gate | Check | Pass |
|---|---|---|
| install | `pip install neuralos` exits 0 | engine + weights on disk |
| import | `import needle; needle.__version__` | version prints |
| **tool call** | `add 2 and 3` via a trigger-caged tool | returns `5` |
| persist | box stopped, `auto_delete=0` | disk survives |

**Measured footprint of a cooked box** (M-series host): idle 178 MB, after one
inference 257 MB, after 8 back-to-back asks 357 MB, disk ~640 MB.
→ **1 vCPU / 1 GB / 4–6 GB disk is sufficient** for neuralOS-only boxes.

---

## Phase 2 — SPIN UP jobs by cloning the template

`clone_job.py` clones the template (copy-on-write → near-instant), starts it,
runs a neuralOS program in the clone, prints the result, stops it.

```bash
$PY $SKILL/scripts/clone_job.py --template neuralos-template \
    --name job-001 \
    --prompt "what's it like in Lagos right now?"
# add --keep to leave the job box running; --program my_tools.py to swap tools
```

- Cloning is **safe for neuralOS** because it is stateless — no identity to
  duplicate.
- **NEVER clone a box that carries unique persisted identity** (e.g. a gateway
  with a generated mesh keypair). Cloning duplicates the identity → collision.
  Treat those boxes as singletons.
- Default in-box program defines demo tools with **triggers** and returns a
  small JSON digest. Swap in your own with `--program file.py` (the file may
  use `__PROMPT__`, replaced with the prompt).

### Warm pool (latency-critical)

Pre-clone N boxes stopped; on demand `start → exec → stop`. Clone + boot is
~2 s end-to-end on this host; a warm pool removes even that from the hot path.

---

## Phase 3 — Data-source instance variant (the chinook pattern)

To give a box a **database-backed neuralOS instance that auto-loads at boot**,
follow the chinook build. Deliverables inside the box (`/opt/<instance>`):

| File | Role |
|---|---|
| `models.py` | strict Pydantic models (from the `neuralos` skill profiling) |
| `needle_menu.json` | probe menu — triggers + grammar-caged args |
| `bridge.py` | reads the source, validates rows, keeps results small |
| `instance.py` | the agent: menu loaded, triggers set |
| `ask.py` | **retrieval front-end** — lexical top-K probe selection |
| `demo_server.py` | web service (`POST /ask`) — the boot service |
| `start.sh` | **boot script** (see below) |
| `<source dump>` | data, loaded on first boot |

### Boot script template (this is the contract)

```sh
#!/bin/sh
# runs as PID 1 on EVERY start
set +e
export PATH="/usr/sbin:/usr/local/bin:/usr/bin:/bin:$PATH"
LOG=/var/log/<instance>.log
exec >>"$LOG" 2>&1                       # PID-1 logs must go somewhere

# 1. init data store datadir if empty; 2. start it in background;
# 3. wait until it answers;  4. ensure app user;  5. load dump ONCE
#    (guard: skip if a sentinel table already exists);
# 6. cd /opt/<instance> && exec python3 demo_server.py --port 8877
# fallback (missing files / failed deps):  exec sleep infinity
```

Create the box with:

```python
boxlite.BoxOptions(image="python:3.12-slim", auto_delete=0,
                   cpus=1, memory_mib=1024, disk_size_gb=6,
                   cmd=["sh","-c","/opt/<instance>/start.sh || sleep infinity"])
```

The `|| sleep infinity` is **essential**: during setup (before files exist)
the box must stay up so you can `exec` installs and `copy_in` files. On the
next start the real script runs end-to-end.

### Verify (Phase-4 gates, never skip)

1. **Data loaded**: query the sentinel table count directly in the store.
2. **Instance answers**: `ask.py <dir> "known question"` → correct probe + truth.
3. **Truth check**: relayed number == direct source query (e.g. Track = 3503).
4. **Boot persistence**: stop → start → service up **and** data still there
   (no re-import, no duplicates).

**Measured (chinook: MariaDB + neuralOS + 48-probe instance)**:
boot-to-serving ~10 s; idle 178 MB; after inference 257 MB; 8 asks back-to-back
357 MB; disk 642 MB → **1 vCPU / 1 GB / 6 GB**. A 2 vCPU / 4 GB / 12 GB build
used <9 % of its RAM — don't over-allocate.

---

## Keeping a box RUNNING (three verified mechanisms)

By default a box **auto-stops when the owning client process exits** ("Auto-
stopping non-detached box"). Verified ways to keep one up:

**1. Keeper process** — a background client that holds the runtime open
(`scripts/keeper.py`, run under nohup; touch `keeper.stop` to stop it).
Works, but see gotcha 14: the keeper holds the **runtime lock**, so no other
process can use `~/.boxlite` until it exits.

**2. `detach=True` (recommended)** — create the box detached and it
**outlives every client**: `BoxOptions(..., detach=True)`. A fresh client can
`rt.get()` + `exec` against the already-running box in ~1.6 s. Verified:
client exited, box's shim + demo_server kept running, next client attached
and asked successfully. Cannot be set on an existing box — rebuild to change.

**3. `boxlite serve` + REST** — the CLI distribution (separate from the pip
SDK: `curl -fsSL https://sh.boxlite.ai | sh`) runs a REST control plane that
keeps boxes alive server-side and lets *many* clients share them:

```bash
nohup ~/.local/bin/boxlite serve --api-key <key> > serve.log 2>&1 &
# SDK client — note: Boxlite.rest is NOT a coroutine, do not await it
rt = boxlite.Boxlite.rest(boxlite.BoxliteRestOptions(
    url="http://localhost:8100",
    credential=boxlite.ApiKeyCredential("<key>")))   # ApiKeyCredential, not str
for i in await rt.list_info(): ...                   # status/pid visible here
box  = await rt.get("chinook-1gb")                   # attach to a running box
```
Plain curl exec: `{"command": "<argv0>", "args": [...]}` — `command` is a
single executable string (wrap shell with `"sh","-c"` args), not a shell line.

## Phase 4 — Portability (no registry)

```bash
$PY $SKILL/scripts/export_box.py --name neuralos-template \
    --dest ~/boxlite-lab/archives/neuralos-template.boxlite   # ~81 MB
$PY $SKILL/scripts/import_box.py \
    --archive ~/boxlite-lab/archives/neuralos-template.boxlite \
    --name neuralos-template            # on any BoxLite-capable host
```

---

## API cheat sheet (boxlite 0.10.4, verified)

```python
import asyncio, boxlite

async def main():
    rt = boxlite.Boxlite.default()

    # create / get (get_or_create returns a TUPLE — unwrap it)
    got = await rt.get_or_create(boxlite.BoxOptions(
        image="python:3.12-slim", auto_delete=0, cpus=1, memory_mib=1024,
        disk_size_gb=6, cmd=["sh","-c","/opt/app/start.sh || sleep infinity"]),
        name="my-template")
    box = got[0] if isinstance(got, tuple) else got

    box = await rt.get("my-template")            # by name or id
    await box.start()                            # boots; runs cmd as PID 1
    ex  = await box.exec("python", ["-c", "print('hi')"], timeout_secs=600)
    out = []
    async for line in ex.stdout():               # MUST drain streams
        out.append(line)                         # BEFORE wait()
    res = await ex.wait()                        # ExecResult: .exit_code
    await box.stop()                             # handle INVALIDATED after stop

    clone  = await box.clone_box(name="job-1")   # CoW clone, INHERITS shape
    await box.export(dest="/path/archive.boxlite")
    box2  = await rt.import_box("/path/archive.boxlite", name="restored")
    infos = await rt.list_info()                 # await it
    await rt.remove("job-1")                     # await it
asyncio.run(main())
```

---

## Gotchas — every one hit in practice

1. **`get_or_create` returns a tuple** → `got[0] if isinstance(got, tuple) else got`.
2. **`stop()` invalidates the box handle** → `rt.get(name)` again before restart.
3. **`Box.exec` streams**: drain `ex.stdout()` with `async for` **before**
   `await ex.wait()`, else *"stdout stream not available"*. (`SimpleBox.exec`
   instead returns a buffered `ExecResult` with `.stdout` directly.)
4. **`PublishedPort` cannot be constructed from Python** (0.10.4) — don't plan
   on SDK port publication; reach in-box services via `exec`, or `network tunnel`.
5. **`CloneOptions` is a placeholder** — `clone_box` **inherits the parent's
   resources**; you cannot resize via clone. Rebuild to change shape.
6. **`copy_in(dir, dest)` includes the parent dirname** → `/src/chinook` to
   `/opt` lands at `/opt/chinook/…`. A file copies to `dest/<filename>`.
7. **No `rename`.** Names are unique and picked at create; duplicates raise.
   Use `get_or_create` for idempotency.
8. **Box ID is runtime-generated** — you cannot set it. Use names as your
   stable handle.
9. **Guest IP/MAC/hostname are identical in every box** (per-box netstack).
   Identify boxes by name/id only.
10. **PID 1 must never exit** during setup — hence `cmd=["sh","-c",
    "<script> || sleep infinity"]`.
11. **Python 3.14** lacks the wheels; use 3.11–3.13 (3.12 recommended).
12. **neuralOS hygiene** (from the `neuralos` skill — non-negotiable):
    triggers on every tool; grammar-caged args; small results (digest, not rows);
    never secrets as tool arguments; ≤2 asks per turn; disable telemetry with
    `NEEDLE_TELEMETRY=0` + `DO_NOT_TRACK=1`.
13. **MySQL→MariaDB dumps** (data instances): re-dump with
    `--set-gtid-purged=OFF` and `sed 's/utf8mb4_0900_ai_ci/utf8mb4_general_ci/g'`
    — MariaDB rejects both MySQL-8 artefacts.
14. **ONE runtime per BOXLITE_HOME.** A second process touching `~/.boxlite`
    panics with *"Another BoxliteRuntime is already using directory"*. A keeper
    holding a box blocks all other SDK processes — use `detach=True` or
    `boxlite serve` for multi-client access. After killing a holder, the lock
    can linger briefly; retry, or `kill -9` the stale holder.
15. **`Boxlite.rest(...)` is NOT a coroutine** (plain call), and its
    `credential=` takes an **`ApiKeyCredential` object**, not a string.
16. **`start()` has no `detach` kwarg** — detach is a *creation-time*
    `BoxOptions` field; existing boxes cannot be converted (rebuild instead).
17. The REST API exposes real **status/pid** fields per box (the SDK's
    `list_info()` objects do not surface them usefully — use REST for
    liveness checks).
18. Watch for **stray host processes** when testing services on fixed ports:
    an old `demo_server.py --port 8877` on the *host* (left from pre-box work)
    does not conflict with the in-box server (separate netns) but will confuse
    host-side curls.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `no running event loop` / `cannot be cast as …` | calling sync-looking SDK methods outside `asyncio` / wrong options type | wrap in `asyncio.run`; `clone_box` takes `CloneOptions`, not `BoxOptions` |
| box "disappears" after use | `SimpleBox` async-context auto-deletes | use `rt.create` + `auto_delete=0` for anything you keep |
| `stdout stream not available` | waited before draining | `async for` on the stream first (see cheat sheet) |
| instance service dies, box stops | PID-1 exits | `cmd = ["sh","-c","<start.sh> \|\| sleep infinity"]` |
| "Unknown database"/collation errors on import | MySQL-8 dump into MariaDB | gotcha 13 |
| box re-imports/duplicates data every boot | load step not guarded | sentinel-table check before import |
| wheel install fails | Python 3.14 host | rebuild venv with 3.12 |
| `Another BoxliteRuntime is already using directory` | two clients, one BOXLITE_HOME | stop the other holder, or move to detach/serve (gotcha 14) |
| box stops when my script ends | non-detached lifecycle | `detach=True` at create, keeper, or `boxlite serve` |
| `cannot be cast as ApiKeyCredential` | REST credential passed as str | `boxlite.ApiKeyCredential("key")` |
| `await Boxlite.rest(...)` TypeError | rest() is sync | drop the await |

`scripts/keeper.py` holds a box up (mechanism 1). It also serves as the
template for any long-lived holder.

## Fleet operations

| Script | Purpose |
|---|---|
| `scripts/warm_pool.py` | maintain N warm CoW clones of the template; `--dispense` prints one |
| `scripts/upgrade.sh` | rollback archive → in-place upgrade → suite gate → auto-rollback on failure |
| `scripts/backup.sh` | timestamped archive export with retention (`--keep N`) |
| `scripts/keeper.py` | hold a box up (mechanism 1 of 3; see Keeping a box RUNNING) |

**Egress lockdown**: data instances need zero network at runtime. Install
phase requires network; for locked-down jobs create the template with a
NetworkSpec deny (`allow_net`) or bake an offline OCI image — 0.10.4's
`CloneOptions` carries no network fields, so lockdown is set at
create/template time.

## Deliverables contract

A completed run leaves: the **template box** (verified), the **job boxes**
(or archives), and a **verification record** — install gate, tool-call gate,
per-job expected-result checks, and for data instances the truth-check +
boot-persistence evidence. If any gate is unverified, the job is not done.
