# neuralosd — deterministic-first agentic runtime

**One pip install. Two sandbox backends. Any data source. Offline. On CPU.**

`neuralosd` is the agentic runtime for [neuralOS](https://neuralos.ng) — the
121M-parameter on-device tool-calling model. It turns any data source into a
private, offline question-answering service inside hardware-isolated microVMs.

```bash
pip install neuralosd[all]
```

## Single-file binaries (no Python required)

Prebuilt standalone binaries are attached to every
[release](https://github.com/DrOlu/neuralosd/releases) — the Python runtime,
the framework, the docs and the skills are all embedded in one file:

| Platform | Download |
|---|---|
| macOS Apple Silicon | `neuralosd-macos-arm64` |
| macOS Intel | `neuralosd-macos-x64` |
| Linux x86_64 | `neuralosd-linux-x64` |
| Linux ARM64 | `neuralosd-linux-arm64` |
| Windows x64 | `neuralosd-windows-x64.exe` |

```bash
curl -L -o neuralosd https://github.com/DrOlu/neuralosd/releases/latest/download/neuralosd-linux-x64
chmod +x neuralosd
./neuralosd init --source data.csv --name mydata
./neuralosd ask --instance-dir ./mydata "how many rows"
```

The first run extracts the payload once (~10 s); later runs are cached and
start in ~0.25 s. Built with [Nuitka](https://nuitka.net) and verified by a
12-check CLI smoke suite on every platform in CI.

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
