# HTTP API

`neuralosd serve` exposes the runtime over an authenticated `/v1` API. It is
the same engine as the CLI: every answer comes from an allowlisted probe, a
refusal carries its reason, and the model never applies an action. The
OpenAPI 3.1 contract is at `GET /openapi.json`; a self-contained reference
(no CDN) is at `GET /docs`.

```bash
neuralosd serve --instance-dir ~/neuralos-instances/plasma-estate \
    --host 127.0.0.1 --port 9998 --api-key "$NEURALOSD_KEY"
```

Without `--api-key`, the lab key `neuralosd` is used. A non-loopback bind is
refused unless a non-default key is configured.

## Authentication

Send either header on every request except `/healthz`, `/openapi.json` and
`/docs`:

```bash
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:9998/v1/instances
curl -H "X-API-Key: $KEY"             http://127.0.0.1:9998/v1/instances
```

- Constant-time comparison; a query-string key is never accepted.
- `--api-key-file` reads one key per line; `NEURALOSD_SERVE_API_KEY` (or the
  legacy `NEURALOSD_API_TOKEN`) also works.
- The key is never logged. Audit rows carry `key_id`: the first 12 hex
  characters of its SHA-256.

## Read-only mode

`--read-only` refuses every mutating endpoint — `apply`, `undo`, `reload` —
with `403 forbidden`. Reads and dry-runs (`{"dry_run": true}` on apply/undo)
still work.

## TLS

```bash
neuralosd serve ... --tls-cert server.crt --tls-key server.key
```

Both flags are required together; the listener is wrapped with a standard
TLS context. Terminate at a proxy if you prefer.

## Limits and timeouts

| Limit | Default | Flag |
|---|---|---|
| Per-call timeout | 300 s | `--timeout` |
| Request body | 1 MiB (`413`) | `--max-body` |
| Response output | 8 MiB (`500 internal`) | `--max-output` |
| Plan validity | 24 h (`410` after) | `--plan-ttl` |
| Concurrent requests | 64 (`503` overflow) | — |

`POST /ask` accepts `options.timeout_ms` (bounded by `--timeout`) and
`options.max_results`.

## Audit

Every authenticated request that names a loaded instance — successes and
failures alike, except rejected authentication — appends a row to
`<instance>/audit.jsonl`:

```json
{"event_id": "evt_...", "ts": 1790000000.0, "request_id": "req_...",
 "remote": "10.0.0.5", "key_id": "9f2c1a7b3d40", "method": "POST",
 "path": "/v1/instances/plasma-estate/ask", "instance": "plasma-estate",
 "probe": "health", "plan_id": null, "outcome": "answered", "status": 200,
 "duration_ms": 12, "refusal_reason": null,
 "question_sha256": "5d4f...", "prev": "00", "hash": "ab12..."}
```

- The raw question is **never** stored, only its SHA-256.
- Rows are hash-chained (`prev` + canonical row → `hash`); verify with
  `GET /v1/instances/{instance}/audit/verify`. A tampered row reports
  `{"ok": false, "broken_at": N}`.
- `GET /audit` filters by `outcome`, `since`, `limit`, `cursor`;
  `GET /refusals` lists recent refusals (question replaced by a hash).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness (no auth) |
| GET | `/v1/version` | Version, engine, model, gate/menu versions |
| GET | `/v1/instances` | List loaded instances |
| GET | `/v1/instances/{instance}` | Instance metadata/status |
| GET | `/v1/instances/{instance}/health` | Health/readiness |
| GET | `/v1/instances/{instance}/capabilities` | Capability list |
| GET | `/v1/instances/{instance}/menu` | Probe menu |
| GET | `/v1/instances/{instance}/menu/{probe}` | One probe's schema |
| GET | `/v1/instances/{instance}/gate` | Gate version, reasons, rules |
| GET | `/v1/instances/{instance}/model` | Model metadata |
| GET | `/v1/instances/{instance}/golden` | Golden cases |
| GET | `/v1/instances/{instance}/config` | Redacted configuration |
| GET | `/v1/instances/{instance}/stats` | Counts |
| POST | `/v1/instances/{instance}/ask` | Ask a question |
| POST | `/v1/instances/{instance}/confirm/{token}` | Complete a HITL confirmation |
| POST | `/v1/instances/{instance}/probes/{probe}/run` | Typed probe execution |
| GET | `/v1/instances/{instance}/requests/{request_id}` | Retrieve a prior result |
| GET | `/v1/instances/{instance}/requests/{request_id}/envelope` | Retrieve its envelope |
| GET | `/v1/instances/{instance}/plans` | List plans |
| GET | `/v1/instances/{instance}/plans/{plan_id}` | One plan |
| POST | `/v1/instances/{instance}/plans/{plan_id}/apply` | Apply a proposed plan |
| POST | `/v1/instances/{instance}/plans/{plan_id}/undo` | Undo an applied plan |
| GET | `/v1/instances/{instance}/plans/{plan_id}/events` | Plan lifecycle |
| GET | `/v1/instances/{instance}/audit` | Audit rows |
| GET | `/v1/instances/{instance}/audit/verify` | Verify the chain |
| GET | `/v1/instances/{instance}/audit/{event_id}` | One audit row |
| GET | `/v1/instances/{instance}/refusals` | Recent refusals |
| POST | `/v1/instances/{instance}/reload` | Reload menu/config (mutating) |
| GET | `/v1/instances/{instance}/metrics` | Prometheus-style metrics |
| GET | `/openapi.json` | OpenAPI 3.1 document (no auth) |
| GET | `/docs` | HTML reference (no auth) |

`/v1/instances/{instance}/health` runs the instance's first trigger inside the
readiness budget, `?timeout_s=` (default 10, clamped to 1–120). A gate refusal
counts as ready — the runtime refused, it is not down. When a live backend is
slower than the budget (a cloud API on a cold start), health answers `200`
with `"degraded": true` instead of 503: the menu, gate and model are loaded
and cached answers still work.

## Ask

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"question":"is the estate healthy",
       "options":{"include_envelope":true,"max_results":10}}' \
  http://127.0.0.1:9998/v1/instances/plasma-estate/ask
```

```json
{"ok": true, "request_id": "req_...", "instance": "plasma-estate",
 "probe": "health", "mode": "deterministic", "refusal_reason": null,
 "results": [{"healthy": true}], "gate_version": 1,
 "menu_version": "272811b192f4", "duration_ms": 12}
```

A refusal is `422` with `ok: false`, `code: "refused"`, the bare
`refusal_reason`, and the gate/menu versions. The reason is never translated
into a guess. The four reasons are `no_probe_matches`, `action_intent`,
`dropped_filter` and `low_coverage` (plus the other gate tokens documented in
the [reference](reference.md)).

## Probes

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"args":{"host":"WS1"}}' \
  http://127.0.0.1:9998/v1/instances/plasma-estate/probes/health/run
```

- Unknown probe → `404`; invalid/missing/extra args → `400`.
- A `confirm=True` probe returns `409` from `/run`; ask it through `/ask`,
  which returns `409` with a `confirm_token` for `/confirm/{token}`.
- The probe executes through the same data layer; PII masking and
  JSON-normalisation apply.

## Plans

Plans are the actuation journal: `proposed` (dry-run) → `applied` → `undone`.
`apply` requires `proposed`; `undo` requires `applied`; everything else is
`409`. A proposed plan older than `--plan-ttl` is `410`. `apply`/`undo` are
serialized per instance and accept `Idempotency-Key`; a repeated key returns
the same result with `"replayed": true`. The API never auto-applies.

```bash
curl -s -X POST -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: approval-42' -H 'Content-Type: application/json' \
  -d '{"reason":"operator approved"}' \
  http://127.0.0.1:9998/v1/instances/plasma-estate/plans/p_01H.../apply
```

`dry_run: true` returns `would_apply`/`would_undo` without a transition and
is allowed in read-only mode.

## Maintenance and operator endpoints

A **fixed allowlist** of typed operations — never a generic shell. Reads are
allowed in `--read-only`; anything that writes (`golden/run`,
`shadow/{name}/evaluate`, `shadow/{name}/promote`, `mothball` with
`apply=true`, `GET synonyms` — it writes its proposal file, `synonyms/apply`,
`calibrate`, `snapshot`, `reload`) is refused with `403`. Dry-runs (plan
`apply` with `dry_run=true`, `mothball` without `apply`) remain allowed.

| Method | Path | Function |
|---|---|---|
| GET | `/v1/instances/{instance}/lint` | Trigger-collision lint (`?top=8`) |
| GET | `/v1/instances/{instance}/gaps` | Mine refused/partial questions from the ask ledger, clustered |
| POST | `/v1/instances/{instance}/golden/run` | Replay the golden bank in the self-test ledger |
| GET | `/v1/instances/{instance}/shadow` | Shadowed-metric report |
| POST | `/v1/instances/{instance}/shadow/{name}/evaluate` | Evaluate one shadowed metric against its oracle |
| POST | `/v1/instances/{instance}/shadow/{name}/promote` | Promote after agreement (`min_agreement`, `min_runs`, `max_age_days`) |
| POST | `/v1/instances/{instance}/mothball` | Demote stale probes; `{"age_days":180,"apply":false}` |
| GET | `/v1/instances/{instance}/synonyms` | Propose synonym triggers from refusals |
| POST | `/v1/instances/{instance}/synonyms/apply` | Apply approved proposals (`{"proposals":[…]}` or the saved proposal file) |
| POST | `/v1/instances/{instance}/calibrate` | Fit refusal thresholds from the golden/trap banks; reloads the instance |
| GET | `/v1/instances/{instance}/catalog` | `CATALOG.md` (or generated Markdown) |
| GET | `/v1/instances/{instance}/openapi` | The probe-level OpenAPI document |
| GET | `/v1/instances/{instance}/slo` | Refusal SLO (`window_hours`, `max_refusal_rate`, `min_asks`) |
| GET | `/v1/instances/{instance}/snapshots` | Pinned observation snapshots |
| POST | `/v1/instances/{instance}/snapshot` | Pin a snapshot (`{"label":"q3"}`) |
| GET | `/v1/system/backends` | BoxLite/Microsandbox availability |
| GET | `/v1/system/doors` | Registered sandbox doors |
| POST | `/v1/instances/{instance}/mesh/discover` | Discover mesh peers matching a question |
| POST | `/v1/instances/{instance}/mesh/preflight` | Signed local would-you-answer preflight |
| POST | `/v1/instances/{instance}/mesh/delegate` | Delegate a question to a named peer |

Mesh endpoints are **off by default**: start with `--allow-mesh` to enable
them, and configure the gateway through `REACTORPRO_GATEWAY_URL` or the
request's `gateway`. Delegation respects the mesh trust policy and loop/hop
limits. `deploy` and `door` remain CLI-only: publishing ports and creating
sandboxes from a web API is a different blast-radius class.

## Errors

```json
{"ok": false, "code": "not_found", "error": "unknown instance: x",
 "request_id": "req_..."}
```

Codes: `unauthorized`, `forbidden`, `not_found`, `invalid_request`,
`refused`, `conflict`, `gone`, `timeout`, `internal`. Every response carries
`X-Request-Id`; the body carries `request_id` except `/openapi.json`,
`/docs` and `/metrics`.

## Multi-instance

`--instance-dir` is repeatable; the directory basename is the instance name:

```bash
neuralosd serve --instance-dir ~/instances/plasma-estate \
                --instance-dir ~/instances/finance --port 9998
```

Each instance has its own menu, plans, audit and model. Unknown instance →
`404`. (The legacy `--instances-dir` / `/{name}/ask` mode still exists for
backwards compatibility.)

## Deployment

A minimal systemd unit:

```ini
[Unit]
Description=neuralOSd API
After=network.target

[Service]
ExecStart=/usr/local/bin/neuralosd serve \
  --instance-dir /srv/neuralos/plasma-estate \
  --host 127.0.0.1 --port 9998 --api-key-file /etc/neuralos/api-keys \
  --read-only
Environment=NEURALOSD_SERVE_API_KEY=
Restart=on-failure
User=neuralos

[Install]
WantedBy=multi-user.target
```

Put TLS or an authenticating proxy in front, keep `--read-only` until the
plan workflow is approved, and rotate keys with `--api-key-file`.

## CLI equivalents

| API | CLI |
|---|---|
| `GET /v1/instances/{i}/menu` | `neuralosd openapi <menu>` |
| `GET /v1/instances/{i}/golden` | `neuralosd golden --dir` |
| `GET /v1/instances/{i}/gate` | `neuralosd doctor` / `neuralosd docs reference` |
| `GET /v1/instances/{i}/audit` | `neuralosd audit` / `neuralosd scrub` |
| plans apply/undo | `neuralosd actuate --apply/--undo` |
| `POST /ask` | `neuralosd ask` |
| `POST /probes/{p}/run` | typed probe call via the SDK |
