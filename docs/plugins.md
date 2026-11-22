# Plugins

A plugin adds capabilities to neuralOSd without forking the core. Five kinds
share one manifest and one registry:

| Kind | What it adds |
|---|---|
| **Probe pack** | Questions the instance can answer |
| **Source** | A new `--source` type / connector |
| **Model provider** | Routing fallback: on-device, Ollama, OpenAI-compatible, custom |
| **Sandbox backend** | A new isolation tier alongside BoxLite/MSB |
| **Audit sink / policy hook** | A destination for audit rows; a refusal-only gate hook |

The core keeps ownership of the refusal gate, the signed envelope and the
audit chain. A plugin can add a capability; it cannot remove a refusal or
invent an answer.

## Quick start

```sh
# Scaffold a probe pack
neuralosd plugin new acme --kind probes --out ./acme
neuralosd plugin test ./acme

# Enable it for one instance
neuralosd plugin enable acme --instance-dir ./sales --path ./acme

neuralosd ask --instance-dir ./sales "acme hello"
```

`enable` writes `plugins.json` in the instance:

```json
{
  "plugins": [
    {"name": "acme", "path": "/abs/path/acme", "enabled": true,
     "config": {}}
  ],
  "model_provider": "ollama:llama3.1",
  "allow": ["acme"]
}
```

`model_provider` selects the routing fallback (see below). `allow` is an
operator allowlist: when present, any plugin not named is refused.

## Manifest

```json
{
  "schema": "neuralosd.plugin/1",
  "name": "acme",
  "version": "1.0.0",
  "plugin_api": "1",
  "neuralosd": ">=3.6,<4",
  "kinds": ["probes"],
  "runtime": "inprocess",
  "entrypoint": "plugin:register",
  "permissions": {"timeout_seconds": 30},
  "config_schema": {"type": "object"},
  "description": "Acme probe pack",
  "integrity": "sha256:…",
  "signature": "plugin.sig",
  "public_key": "plugin.pub"
}
```

- `plugin_api` is checked against the runtime (currently `1`).
- `neuralosd` is a version range (`>=3.6,<4`).
- `runtime` is `inprocess` (Python entrypoint) or `process`
  (newline-delimited JSON-RPC over stdin/stdout; `command` is required).
- `permissions.timeout_seconds` bounds an out-of-process call.

## Probe packs

```python
from neuralosd import probe

@probe(description="Acme invoices", triggers=["acme invoices", "acme billing"])
def invoices():
    return {"invoices": 12, "paid": 9}

def register(ctx):
    ctx.register_probe(invoices)
```

The probe is namespaced by the plugin (`acme_invoices`), collision-checked, and
appears in the CLI, HTTP API, MCP and help automatically. Every audit row for
it carries `plugin`, `plugin_version` and `plugin_digest`.

Optional `golden=` and `traps=` lists can be attached at registration.

## Model providers

A provider is a **closed-list router**: it may only name a probe that is
already in the menu and supply arguments the probe declares. The core executes
the probe and applies the results gate, so a provider cannot answer on its own.

```python
from neuralosd.plugins.providers import ModelProvider

class MyProvider(ModelProvider):
    name = "my-router"
    def select(self, question, menu, k=8):
        return {"probe": "acme_invoices", "args": {}}  # or None to abstain

def register(ctx):
    ctx.register_provider("my-router", MyProvider())
```

Built-in providers, selectable with `--model-provider` or `plugins.json`:

```sh
neuralosd ask --instance-dir ./sales --model-provider deterministic "…"
neuralosd ask --instance-dir ./sales --model-provider ollama:llama3.1 "…"
neuralosd ask --instance-dir ./sales \
  --model-provider openai:http://127.0.0.1:8000/v1:llama3.1 "…"
neuralosd serve --instance-dir ./sales --model-provider ollama:qwen3
```

`openai:` works with vLLM, LM Studio, llama.cpp server, Ollama's
OpenAI-compatible endpoint, or a hosted gateway. An API key, when needed, comes
from `OPENAI_API_KEY`; it is never written to the manifest or the audit row.

## Out-of-process plugins

Set `runtime: "process"` and a command:

```json
{
  "runtime": "process",
  "command": ["python3", "plugin.py"],
  "permissions": {"timeout_seconds": 30}
}
```

The host sends one JSON object per line:

```json
{"id": 1, "method": "init", "params": {"config": {}, "permissions": {}}}
{"id": 1, "result": {"probes": [{"name": "hello", "description": "…",
                                  "triggers": ["process hello"], "args": {}}],
                     "providers": [{"name": "fixed"}]}}
{"id": 2, "method": "probe", "params": {"probe": "hello", "args": {}}}
{"id": 2, "result": {"hello": "process"}}
{"id": 3, "method": "select", "params": {"question": "…", "menu": […]}}
{"id": 3, "result": {"probe": "hello", "args": {}}}
```

The process gets a scrubbed environment, a working directory inside the
plugin, a per-call timeout and a 4 MiB line cap. A crash or timeout becomes an
execution error; the core keeps serving its own probes.


## Sources and `init`

A source plugin turns `init` into a plugin-driven scaffold. It must expose:

```python
class Tickets:
    def profile(self, spec):      # optional: validate/metadata
        return {"kind": "tickets", "rows": 7}
    def probes(self, spec):       # required: @probe functions for this spec
        return [ticket_count]

def register(ctx):
    ctx.register_source("tickets", Tickets())
```

```sh
neuralosd init --source plugin:tickets:7 --plugin-dir ./tickets \
  --name tickets --out ./tickets-inst
neuralosd ask --instance-dir ./tickets-inst "ticket count"
```

The generated instance saves `plugins.json` (no allow list, so a later
backend/sink can be added), `source.json` and a `probes.py`
that loads the plugin and calls `probes(spec)` at load time; data access stays
in the plugin.

## Backends and `deploy`

A backend plugin registers a factory whose object implements:

```python
class MyBackend:
    def deploy(self, instance_dir, *, name, host, port):
        return {"url": f"http://{host}:{port}", "name": name}
    @staticmethod
    def available():
        return True
```

```sh
neuralosd deploy --instance-dir ./sales --backend my-backend \
  --plugin-dir ./my-backend --name sales-edge --port 8501
```

`deploy` resolves the name through the registry, checks `available()`, and
calls `deploy(...)`; a returned `url`/`door` is printed. The backend owns its
isolation, lifecycle and door semantics.

## Integrity, lock and signatures

```sh
neuralosd plugin lock --instance-dir ./sales      # pin versions + digests
neuralosd plugin install ./acme                   # verify, then copy
```

`plugins.lock` stores each enabled plugin's version and a `sha256` tree digest
of its code. The loader refuses a plugin whose code changed after locking.
`install` verifies `integrity` and an optional detached signature before
replacing anything.

Signing needs only `openssl`; the signed payload is
`neuralosd-plugin:<name>@<version>:<tree-digest>`:

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out key.pem
openssl pkey -in key.pem -pubout -out plugin.pub
printf 'neuralosd-plugin:acme@1.0.0:%s' "$DIGEST" > payload.txt
openssl dgst -sha256 -sign key.pem -out plugin.sig payload.txt
```

Set `signature` and `public_key` in the manifest. A present signature that does
not verify is refused.

## Manifest fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | `neuralosd.plugin/1` |
| `name` | yes | Unique plugin name; probes are namespaced by it |
| `version` | yes | Semver; recorded in `plugins.lock` and audit rows |
| `plugin_api` | yes | `1` (checked at load) |
| `neuralosd` | no | Version range, e.g. `>=3.7,<4` |
| `kinds` | yes | Any of probes, source, model_provider, backend, audit_sink, policy_hook |
| `runtime` | no | `inprocess` (default) or `process` |
| `entrypoint` | inprocess | `module:attribute`; a `register(ctx)` function or Plugin |
| `command` | process | argv list for the plugin process |
| `permissions` | no | `timeout_seconds` (process), declared capabilities |
| `config_schema` | no | JSON Schema for `config` in `plugins.json` |
| `integrity` | no | `sha256:` of the code tree; verified on install |
| `signature` / `public_key` | no | Detached OpenSSL signature over the code digest |

## Plugin API reference

`register(ctx)` receives a `PluginContext`:

| Method | Adds |
|---|---|
| `register_probe(fn, name=None, golden=None, traps=None)` | A `@probe` function |
| `register_provider(name, provider)` | A `ModelProvider` |
| `register_source(name, source)` | A source with `profile(spec)` / `probes(spec)` |
| `register_backend(name, factory)` | A sandbox backend factory |
| `register_audit_sink(sink)` | A `write(row)` callable or callable |
| `register_policy_hook(hook)` | `hook(question, probe, args) -> reason or None` |

`ctx` also exposes `manifest`, `instance_dir`, `config`, `read_only` and a
logger. It does not expose the router, the gate or the audit chain.

**Provider contract**

```python
class ModelProvider:
    name = "my-provider"
    def available(self) -> bool: ...
    def select(self, question, menu, k=8):
        return {"probe": "...", "args": {...}}   # or None to abstain
    def complete(self, messages, **opts) -> str | None: ...
```

The menu entries are `{name, description, args, triggers}`. The returned probe
must be one of those names, and the arguments must satisfy the probe's spec;
otherwise the fallback abstains and the router refuses.

**Source contract**

```python
class MySource:
    def profile(self, spec) -> dict: ...      # optional validation/metadata
    def probes(self, spec) -> list[callable]: ...  # required
```

**Backend contract**

```python
class MyBackend:
    @staticmethod
    def available() -> bool: ...
    def deploy(self, instance_dir, *, name, host, port) -> dict: ...
```

**Sink / hook contract**

```python
def sink(row: dict) -> None: ...                     # masked audit row
def hook(question: str, probe: str, args: dict) -> str | None: ...
```

## Recipes

**Ollama routing fallback**

```sh
neuralosd ask --instance-dir ./sales --model-provider ollama:llama3.1 \
  "which region had the most incidents"
```

Ollama receives the closed menu and must reply with a probe name; the core
executes it. If Ollama is unreachable the ask fails loudly rather than
silently falling back to a guess.

**OpenAI-compatible (vLLM, LM Studio, llama.cpp)**

```sh
export OPENAI_API_KEY=...        # only if your server needs it
neuralosd serve --instance-dir ./sales \
  --model-provider openai:http://127.0.0.1:8000/v1:qwen2.5-7b
```

**A probe pack**

```python
from neuralosd import probe

@probe(description="Refund count", triggers=["refund count"])
def refunds():
    return {"refunds": 4}

def register(ctx):
    ctx.register_probe(refunds)
```

**A source-backed instance**

```sh
neuralosd init --source plugin:tickets:7 --plugin-dir ./tickets \
  --name tickets --out ./tickets-inst
```

**A backend**

```sh
neuralosd deploy --instance-dir ./sales --backend my-backend \
  --plugin-dir ./my-backend --name sales-edge --port 8501
```

**An audit sink + refusal hook**

```python
def register(ctx):
    ctx.register_audit_sink(lambda row: open("audit.jsonl", "a").write(
        __import__("json").dumps(row) + "\n"))
    ctx.register_policy_hook(
        lambda q, probe, args: "off_hours" if probe == "refunds" else None)
```

## Publishing checklist

1. `neuralosd plugin test DIR` passes.
2. Manifest has `name`, `version`, `plugin_api`, `kinds`, `neuralosd`.
3. Probe names are unique and meaningful after namespacing.
4. `neuralosd plugin lock --instance-dir INST` pins the code digest.
5. Optional: sign the digest with `openssl`; set `signature`/`public_key`.
6. `neuralosd plugin install DIR` verifies before copying.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `not in plugins.json allow list` | Add the name to `allow`, or remove the list |
| `locked version … does not match` | Re-run `plugin lock` after a deliberate upgrade |
| `integrity does not match plugins.lock` | Code changed after locking; re-lock |
| `signature verification failed` | Re-sign the code digest or check `public_key` |
| `plugin call timed out` | Raise `permissions.timeout_seconds` or fix the plugin |
| A provider always abstains | It returned a probe not in the menu or invalid args |
| Source init says “no plugin source” | Pass `--plugin-dir`, and check `plugin sources` |

## Limits

- In-process plugins share the runtime's privileges.
- Out-of-process isolation is process-level (scrubbed env, timeout, line cap),
  not a VM; use a sandbox backend for hostile code.
- Source-generated probes are not auto-namespaced; give them unique names.
- Policy hooks cannot force an answer; only the core gate can.
- A plugin cannot alter the signed envelope or the audit hash chain.

## Security model

- Plugins are code with the privileges of the runtime; enable only what you
  reviewed. Out-of-process plugins are the lighter-trust option.
- A provider cannot invent a probe or arguments; the results gate, masking and
  audit still run.
- Policy hooks are refusal-only: they can add a refusal, never remove one.
- `--read-only` serving does not install or enable plugins.
- The model is never in the action path.

## CLI

```sh
neuralosd plugin list [--instance-dir DIR] [--json]
neuralosd plugin inspect PATH
neuralosd plugin test PATH
neuralosd plugin new NAME --kind probes|model_provider --out DIR
neuralosd plugin enable NAME --instance-dir DIR --path PLUGIN_DIR
neuralosd plugin disable NAME --instance-dir DIR
neuralosd plugin sources|providers
neuralosd plugin doctor --instance-dir DIR
neuralosd plugin lock --instance-dir DIR
neuralosd plugin install PLUGIN_DIR
```

See [CLI reference](cli.md) for every flag and example.
