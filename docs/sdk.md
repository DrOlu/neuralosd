# neuralosd as an SDK — embed a verified data brain in your product

Your product doesn't need to know anything about AI. You hand neuralosd a
data source and a menu of questions it can answer, and you get back one
method: `ask(question) → verified answer, or an honest refusal`.

## Two pieces

| Piece | What it is |
|---|---|
| **Server** | `neuralosd serve` — one instance (or `--instances-dir` for many), REST/JSON, optional token auth |
| **Clients** | `neuralosd_sdk` (Python, ships in this package) and `clients/javascript/neuralosd-client.mjs` (zero-dep ESM) — typed calls, retries, refusals as exceptions |

## Quick start

```bash
# server, with auth ON
neuralosd serve --instance-dir ./my-instance --port 8877 \
                --auth-token "$NEURALOSD_API_TOKEN"
```

```python
from neuralosd_sdk import NeuralOSDClient, RefusalError

client = NeuralOSDClient("http://127.0.0.1:8877", token="…")
try:
    r = client.ask("total revenue by region")
    print(r.probe, r.results)          # verified data
except RefusalError as e:
    show_user("I can't answer that")   # the instance honestly declined
```

```javascript
import { NeuralOSDClient, RefusalError } from "./neuralosd-client.mjs";
const c = new NeuralOSDClient("http://127.0.0.1:8877", { token: "…" });
try { const r = await c.ask("total revenue by region"); }
catch (e) { if (e instanceof RefusalError) /* honest decline */; }
```

## Error model — refusals are values, not crashes

| Client exception | HTTP | Meaning |
|---|---|---|
| `AuthError` | 401 | token missing/wrong |
| `RefusalError` | 422 | instance cannot answer confidently (`.envelope.reason`) |
| `TransportError` | — | unreachable, or 5xx after retries |

Retries: connection errors and 5xx retry with exponential backoff; 4xx never
retried (retrying a refusal would be asking for a hallucination).

## Auth

`--auth-token` (or `$NEURALOSD_API_TOKEN`) requires `Authorization: Bearer
<token>` (or `X-API-Token`) on every endpoint **except** `/healthz` and
`/ready` — liveness probes must not need credentials and expose only the
instance name. Token comparison is constant-time.

## Deployment shapes

1. **In-process (Python apps):** `from neuralosd.instance import Instance` —
   no server at all.
2. **Sidecar service:** your backend is the only client of `serve`; your
   users never see the port.
3. **AI-native:** `neuralosd openapi ./my-instance` emits an OpenAPI + MCP
   manifest so any AI product consumes the instance as caged, verified tools.
