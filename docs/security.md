# Security model

What neuralosd trusts, what it verifies, and where the boundary is.

## Trust boundaries

- **The instance directory is code.** `probes.py` and `bridge.py` are
  imported and executed with the process's privileges. Treat an instance
  directory as trusted input; do not point it at a directory an untrusted
  party can write.
- **`neuralosd build` runs model-authored code.** The verified builder
  checks banks, reload and restore, but it does not sandbox the probe it
  writes. It requires `"admin": true` once an `agents` block exists; do not
  expose it to an untrusted agent.
- **MCP stdio is local.** The host process chooses the `agent_id` it passes.
  The process boundary is the trust boundary.
- **HTTP is the multi-tenant boundary.** Identity, role, PII and budget are
  enforced there (see below).

## Binding and transport

- `neuralosd serve` and `neuralosd serve-multi` bind `127.0.0.1` by default.
- A non-loopback bind is **refused** unless a credential is configured (a
  global token or at least one per-agent token). There is no silent wide
  bind.
- There is no TLS in the server. Terminate TLS at a reverse proxy and send
  the bearer token over that leg.
- Bodies are capped at 1 MB (413), live request threads are capped (503 on
  overflow), and a stalled socket times out after 30 s.

## Credentials and identity

**Operator token.** `--auth-token` or `$NEURALOSD_API_TOKEN`. It is a shared
operator credential: it may act as any agent and select any role. Use it for
a trusted proxy or an operator console, not as a per-user identity.

**Per-agent tokens.** In `acl.json`:

```json
{
  "agents": {
    "alice": {
      "token_sha256": "<sha256 of the token, 64 hex chars>",
      "role": "analyst",
      "pii": false,
      "admin": false,
      "budget": {"calls": 60, "window": 3600}
    }
  }
}
```

Generate the digest (the secret itself is never stored):

```bash
python3 -c "import hashlib; print(hashlib.sha256(b'alice-secret').hexdigest())"
```

With per-agent tokens:

- The token **binds** the identity. A request that presents Alice's token
  and claims `X-Agent-Id: bob` gets `403 agent_mismatch`.
- The role comes from the agent's ACL entry; `X-NeuralOS-Role` cannot
  escalate it.
- The `agents` block is an allow-list: an unlisted agent is denied.
- An agent sees only **its own** `/audit` rows and `/runs/{id}`; the
  operator token sees all.
- `pii: false` lets that agent read fields the instance normally masks.
  Audit and cache storage are always masked, whatever the response policy.
- `budget` is a sliding window of asks per agent.
- `admin: true` is required for `build`, `shadow`, `mothball` and
  `delegate` (MCP tools) once an `agents` block exists.

If **no** `agents` block exists, the instance keeps the trusted-operator
default: any authenticated caller may claim an agent id, admin is on, and
role selection is trusted. That is the laptop/single-operator mode. A
commercial multi-agent deployment must configure `agents`.

## Data protection

- PII is masked in responses by default (key-name hints plus value shapes:
  email, phone, SSN, IBAN, card/account numbers, credentials).
- The audit ledger and the ask cache are masked at write time regardless of
  the response policy.
- Audit rows carry `agent_id`, `session_id`, `run_id` and `source`. Treat
  the ledger as sensitive.
- Session memory and run traces live under `.agentic/` in the instance
  directory, are bounded, and are keyed by sanitised, hash-suffixed ids.

## Human-in-the-loop

A probe declared `confirm=True` does not execute. `ask` returns
`mode: pending_confirmation` with a single-use token (TTL 300 s); the
probe runs only through `POST /v1/confirm/{token}` (or the MCP `confirm`
tool). `invalid`, `expired` and `already_used` are distinct.

## Denial-of-service bounds

- HTTP: 1 MB body, 64 concurrent requests, 30 s socket timeout, bounded
  audit query, bounded identity strings.
- Agent state: bounded session rows, trace events per run, idempotency
  entries, run records, budget maps and cached instance counts.
- The router's row cap and result gate bound probe output.

## Known limits

- Budgets, idempotency, async runs and HITL tokens are **in-memory and per
  process**. Behind multiple workers or replicas they are not shared; use
  sticky sessions or a shared store before scaling out.
- No built-in TLS, authentication proxy, or network rate limit.
- MCP stdio has no authentication; the local process is the boundary.
- `build` executes model-authored Python with host privileges.
- Delegation sends the question to a mesh peer (admin-gated).
- The scheduler runs due jobs synchronously on one thread; there is no
  cross-process overlap protection.

## Reporting

For a suspected vulnerability, contact the maintainers privately rather
than opening a public issue with a reproduction.
