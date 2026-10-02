# neuralosd — deterministic-first agentic runtime

**One pip install. Two sandbox backends. Any data source. Offline. On CPU.**

`neuralosd` is the agentic runtime for [neuralOS](https://neuralos.ng) — the
121M-parameter on-device tool-calling model. It turns any data source into a
private, offline question-answering service inside hardware-isolated microVMs.

```bash
pip install neuralosd[all]

# or with uv (faster, and can supply its own Python)
uv pip install "neuralosd[all]"
uv tool install neuralosd                      # global CLI
uvx neuralosd ask --instance-dir . "how many rows"   # no install at all
```

## Single-file binaries (no Python required)

Every [release](https://github.com/DrOlu/neuralosd/releases) ships standalone
Nuitka binaries in **three variants** — the Python runtime, the framework, the
docs and the skills are all embedded in one file:

| Variant | File | Adds |
|---|---|---|
| base | `neuralosd-<platform>` | framework + docs + skills + on-device model |
| boxlite | `neuralosd-boxlite-<platform>` | + the BoxLite microVM engine |
| msb | `neuralosd-msb-<platform>` | + the Microsandbox `msb` runtime |

Each variant carries **exactly** its own backend — check with
`neuralosd backends`:

```
Sandbox backends:
  boxlite  [available]
  msb      [not installed]
```

| | macos-arm64 | macos-x64 | linux-x64 | linux-arm64 | windows-x64 |
|---|---|---|---|---|---|
| base | ✓ | ✓ | ✓ | ✓ | ✓ |
| boxlite | ✓ | — | ✓ | ✓ | — |
| msb | ✓ | — | ✓ | ✓ | ✓ |

— = no upstream wheel for that platform (boxlite: no Windows/Intel-macOS;
microsandbox: no Intel-macOS).

```bash
# base
curl -L -o neuralosd https://github.com/DrOlu/neuralosd/releases/latest/download/neuralosd-linux-x64
chmod +x neuralosd
./neuralosd init --source data.csv --name mydata
./neuralosd ask --instance-dir ./mydata "how many rows"

# with a sandbox backend
curl -L -o neuralosd-msb https://github.com/DrOlu/neuralosd/releases/latest/download/neuralosd-msb-macos-arm64
chmod +x neuralosd-msb
./neuralosd-msb backends            # msb [available]
./neuralosd-msb deploy --instance-dir ./mydata --name box --backend msb
```

The first run extracts the payload once (~10 s); later runs are cached and
start in ~0.25 s. Built with [Nuitka](https://nuitka.net); every binary is
verified by a 12-check CLI smoke suite plus a strict backend check in CI.

### Beyond what's baked in: the sidecar

A frozen binary cannot import the host's packages — installing a library does
not make it visible. So when a probe needs something the binary doesn't carry,
it can run that probe in the **host Python** instead, via a small helper:

```bash
# on the HOST (not inside the binary):
pip install neuralosd        # provides the `neuralosd-sidecar` command
pip install pypdf pywinrm    # whatever your probes need

# then just use the binary as normal — it delegates automatically
./neuralosd-msb ask --instance-dir ./mydata "pdf pages"
```

**No Python on the machine?** uv can supply one. `neuralosd sidecar --setup`
creates a dedicated environment at `~/.neuralosd/sidecar` — installing a
Python if necessary — and the binary then finds it with **no configuration at
all**:

```bash
neuralosd sidecar --status                      # uv found? provisioned?
neuralosd sidecar --setup --with pypdf,pywinrm  # create it
neuralosd sidecar --setup --force               # rebuild from scratch
```

Provisioning is always explicit — it touches the network, so it never happens
silently while answering a question.

How it decides:

| Situation | Behaviour |
|---|---|
| probe's imports all resolve in the binary | runs **in-process** (fast, no subprocess) |
| probe's import is missing (even lazily, inside the function) | retried in the **sidecar** |
| probe marked `tier="sidecar"` | always runs in the sidecar |
| missing import, no sidecar installed | exits with `pip install <lib>` and the `neuralosd sidecar --setup` hint |

Opt out with `NEURALOSD_SIDECAR=off`, or point at a specific helper with
`NEURALOSD_SIDECAR=/path/to/neuralosd-sidecar`.

So the binary stays **one stable file**, and you never rebuild it to gain a
library — you just add the library to the helper.

## What it does

```
YOUR DATA (CSV, DB, API, logs)     THE RUNTIME                THE RESULT
──────────────────────           ──────────────            ──────────────
profile → model → generate  →   @probe + router    →    ask in English,
                         →   chains + guardrails     get verified
                         →   serve + monitor         answers, offline
```

## Two sandbox backends

| Backend | Install | Best for |
|---|---|---|
| **BoxLite** | `pip install neuralosd[boxlite]` | persistent service boxes, CoW clones, port publication |
| **Microsandbox** | `pip install neuralosd[msb]` | live RAM snapshots, CoW forks, declarative recreate |

Both run the same probe contract. Switch by configuration, not code.

## Quick start

```bash
# 1. Install
pip install neuralosd[all]

# 2. Build an instance from your data
neuralosd init --source your_data.csv --name my-analyst

# 3. Deploy into a sandbox
neuralosd deploy --name my-analyst --backend boxlite

# 4. Ask
neuralosd ask --instance my-analyst "how many records"
```

## The @probe framework

```python
from neuralosd import probe, Instance

@probe(
    description="Customers ranked by lifetime spend",
    triggers=["top customers", "best customers"],
    args={"limit": {"type": "integer", "min": 1, "max": 25, "default": 10}},
    pii=["email"],
)
def top_customers(limit: int = 10):
    return db.query("SELECT ... LIMIT %s", (limit,))

inst = Instance(name="my-analyst", probes=[top_customers])
env = inst.ask("top customers")
print(env["results"])
```

## Chains (multi-step answers)

```python
from neuralosd import chain, ChainRunner

@chain(name="genre_deep_dive", steps=[
    {"probe": "top_genres"},
    {"probe": "tracks_by_genre", "args": {"genre": "{{step_0.genre}}"}},
])
def genre_deep_dive(ctx): ...

runner = ChainRunner(probes_by_name)
runner.register("genre_deep_dive", genre_deep_dive._chain_steps)
result = runner.run("genre_deep_dive")
```

## Meta-selector (multi-instance routing)

```python
from neuralosd import MetaSelector

selector = MetaSelector({
    "finance": {"description": "revenue, invoices, customers"},
    "ops":     {"description": "incidents, uptime, alerts"},
})
instance = selector.route("how many open incidents")  # → "ops"
```

## HITL guardrails

```python
from neuralosd import ConfirmStore
store = ConfirmStore()
pending = store.create(probe_fn, args, reason="low confidence")
# ... human confirms ...
result = store.confirm(pending["confirm_token"])
```

## MCP server (expose probes to any AI agent)

```bash
python3 -m neuralosd.mcp /path/to/instance
# or add to Claude Desktop / Cursor MCP config
```

## License

MIT
