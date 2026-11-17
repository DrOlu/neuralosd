"""The OpenAPI 3.1 contract for the first-class HTTP API.

`ROUTES` is the single source of truth: the server dispatches on it and the
spec is generated from it, so a path cannot exist in one and not the other.
"""
from . import __version__

# (method, path template, tag, summary)
ROUTES = [
    ("GET", "/healthz", "health", "Liveness (no auth)"),
    ("GET", "/v1/version", "discovery", "Runtime version and engine metadata"),
    ("GET", "/v1/instances", "instances", "List loaded instances"),
    ("GET", "/v1/instances/{instance}", "instances",
     "Instance metadata and status"),
    ("GET", "/v1/instances/{instance}/health", "instances",
     "Instance health and readiness"),
    ("GET", "/v1/instances/{instance}/capabilities", "discovery",
     "What this instance can do"),
    ("GET", "/v1/instances/{instance}/menu", "discovery",
     "Full probe menu"),
    ("GET", "/v1/instances/{instance}/menu/{probe}", "discovery",
     "One probe's schema and metadata"),
    ("GET", "/v1/instances/{instance}/gate", "discovery",
     "Refusal gate version, reasons and rules"),
    ("GET", "/v1/instances/{instance}/model", "discovery",
     "On-device model metadata"),
    ("GET", "/v1/instances/{instance}/golden", "discovery",
     "Golden routing cases"),
    ("GET", "/v1/instances/{instance}/config", "instances",
     "Redacted instance configuration"),
    ("GET", "/v1/instances/{instance}/stats", "instances",
     "Counts: probes, plans, refusals, audit rows"),
    ("POST", "/v1/instances/{instance}/ask", "ask",
     "Ask a natural-language question"),
    ("POST", "/v1/instances/{instance}/confirm/{token}", "ask",
     "Complete a pending HITL confirmation"),
    ("POST", "/v1/instances/{instance}/probes/{probe}/run", "probes",
     "Run one typed probe with explicit arguments"),
    ("GET", "/v1/instances/{instance}/requests/{request_id}", "ask",
     "Retrieve a prior ask/probe result"),
    ("GET", "/v1/instances/{instance}/requests/{request_id}/envelope", "ask",
     "Retrieve the signed envelope"),
    ("GET", "/v1/instances/{instance}/plans", "plans", "List plans"),
    ("GET", "/v1/instances/{instance}/plans/{plan_id}", "plans",
     "Get one plan"),
    ("POST", "/v1/instances/{instance}/plans/{plan_id}/apply", "plans",
     "Apply a proposed plan"),
    ("POST", "/v1/instances/{instance}/plans/{plan_id}/undo", "plans",
     "Undo an applied plan"),
    ("GET", "/v1/instances/{instance}/plans/{plan_id}/events", "plans",
     "Plan lifecycle events"),
    ("GET", "/v1/instances/{instance}/audit", "audit", "API audit rows"),
    # Static before parameterised: /audit/verify must not match {event_id}.
    ("GET", "/v1/instances/{instance}/audit/verify", "audit",
     "Verify the audit chain"),
    ("GET", "/v1/instances/{instance}/audit/{event_id}", "audit",
     "One audit row"),
    ("GET", "/v1/instances/{instance}/refusals", "audit",
     "Recent refusals with reasons"),
    ("POST", "/v1/instances/{instance}/reload", "admin",
     "Reload menu/model/config (mutating)"),
    ("GET", "/v1/instances/{instance}/metrics", "admin",
     "Prometheus-style metrics"),
    # Maintenance and operator reads (fixed allowlist; typed, not a shell).
    ("GET", "/v1/instances/{instance}/lint", "discovery",
     "Trigger-collision lint"),
    ("GET", "/v1/instances/{instance}/gaps", "discovery",
     "Mine gated/fuzzy questions from the ask ledger"),
    ("POST", "/v1/instances/{instance}/golden/run", "discovery",
     "Replay the golden bank (self-test ledger)"),
    ("GET", "/v1/instances/{instance}/shadow", "discovery",
     "Shadowed metrics report"),
    ("POST", "/v1/instances/{instance}/shadow/{name}/evaluate", "discovery",
     "Evaluate one shadowed metric against its oracle"),
    ("POST", "/v1/instances/{instance}/shadow/{name}/promote", "admin",
     "Promote a shadowed metric after agreement"),
    ("POST", "/v1/instances/{instance}/mothball", "admin",
     "Demote stale probes (dry-run unless apply=true)"),
    ("GET", "/v1/instances/{instance}/synonyms", "discovery",
     "Propose synonym triggers from refusals"),
    ("POST", "/v1/instances/{instance}/synonyms/apply", "admin",
     "Apply approved synonym proposals"),
    ("POST", "/v1/instances/{instance}/calibrate", "admin",
     "Fit refusal thresholds from golden/trap banks"),
    ("GET", "/v1/instances/{instance}/catalog", "discovery",
     "What this instance can answer (Markdown)"),
    ("GET", "/v1/instances/{instance}/openapi", "discovery",
     "The probe-level OpenAPI 3.1 document"),
    ("GET", "/v1/instances/{instance}/slo", "discovery",
     "Refusal SLO over a window"),
    ("GET", "/v1/instances/{instance}/snapshots", "discovery",
     "Pinned observation snapshots"),
    ("POST", "/v1/instances/{instance}/snapshot", "admin",
     "Pin a snapshot under a label"),
    ("GET", "/v1/system/backends", "admin",
     "Available sandbox backends"),
    ("GET", "/v1/system/doors", "admin",
     "Registered sandbox doors"),
    ("POST", "/v1/instances/{instance}/mesh/discover", "admin",
     "Discover mesh peers matching a question"),
    ("POST", "/v1/instances/{instance}/mesh/preflight", "admin",
     "Local would-you-answer preflight (signed)"),
    ("POST", "/v1/instances/{instance}/mesh/delegate", "admin",
     "Delegate a question to a mesh peer"),
    # Legacy raw-envelope route, kept for the existing HTTP clients/smoke
    # suite. On a loopback server with no explicitly configured key it stays
    # open, matching the pre-3.2 contract; the /v1 surface is always
    # authenticated.
    ("POST", "/ask", "ask", "Legacy raw-envelope ask"),
    ("GET", "/openapi.json", "discovery", "This document (no auth)"),
    ("GET", "/docs", "discovery", "Self-contained HTML reference (no auth)"),
]

TAGS = [
    {"name": "health", "description": "Liveness and readiness."},
    {"name": "instances", "description": "Instance metadata and status."},
    {"name": "discovery", "description": "Version, menu, gate, model, golden."},
    {"name": "ask", "description": "Natural-language ask and request replay."},
    {"name": "probes", "description": "Typed probe execution."},
    {"name": "plans", "description": "Proposed/applied/undone plan lifecycle."},
    {"name": "audit", "description": "Audit rows, chain verification, refusals."},
    {"name": "admin", "description": "Reload and operational metrics."},
]


def route_paths():
    return {path for _method, path, _tag, _summary in ROUTES}


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema):
    return {"application/json": {"schema": _ref(schema)}}


def _responses(ok="Answer", ok_schema="AskResponse", codes=(200, 400, 401,
                                                          403, 404, 409,
                                                          410, 413, 504,
                                                          500)):
    out = {}
    for code in codes:
        if code == 200:
            out["200"] = {"description": ok,
                          "content": _json(ok_schema)}
        else:
            out[str(code)] = {
                "description": {
                    400: "Invalid request", 401: "Unauthorized",
                    403: "Forbidden (read-only mode)",
                    404: "Instance/probe/plan/request not found",
                    409: "Illegal plan transition or concurrent apply",
                    410: "Plan expired", 413: "Request body too large",
                    504: "Call timed out", 500: "Internal error",
                }[code],
                "content": _json("Error"),
            }
    return out


def build_spec(host: str = "127.0.0.1", port: int = 9998) -> dict:
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "neuralOSd HTTP API",
            "version": __version__,
            "description": (
                "The authenticated HTTP surface of the deterministic-first "
                "agentic runtime. Every answer comes from an allowlisted "
                "probe; a refusal is returned with its reason, never "
                "translated into a guess. The model routes; it never applies "
                "an action."),
            "license": {"name": "MIT"},
        },
        "servers": [{
            "url": "http://{host}:{port}",
            "description": "The neuralOSd API server",
            "variables": {
                "host": {"default": host},
                "port": {"default": str(port)},
            },
        }],
        "tags": TAGS,
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "apiKeyAuth": {"type": "apiKey", "in": "header",
                               "name": "X-API-Key"},
            },
            "schemas": _schemas(),
            "examples": _examples(),
        },
        "paths": {},
        "x-neuralosd-gate-version": "{gate_version}",
        "x-neuralosd-menu-version": "{menu_version}",
    }
    for method, path, tag, summary in ROUTES:
        operation = {
            "tags": [tag],
            "summary": summary,
            "operationId": f"{method.lower()}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}",
            "responses": _responses(ok_schema=_ok_schema(path)),
        }
        if "{" in path:
            operation["parameters"] = [
                {"name": name, "in": "path", "required": True,
                 "schema": {"type": "string"}}
                for name in _params(path)
            ]
        if method == "POST":
            body_schema, example = _post_body(path)
            operation["requestBody"] = {
                "required": False,
                "content": {"application/json": {
                    "schema": _ref(body_schema),
                    "example": example}},
            }
        if method == "POST" and path.endswith(("/apply", "/undo", "/reload")):
            operation["parameters"] = operation.get("parameters", []) + [{
                "name": "Idempotency-Key", "in": "header", "required": False,
                "schema": {"type": "string"}}]
        if path not in ("/healthz", "/openapi.json", "/docs"):
            operation["security"] = [{"bearerAuth": []}, {"apiKeyAuth": []}]
        else:
            operation["security"] = []
        spec["paths"].setdefault(path, {})[method.lower()] = operation
    return spec


def _params(path):
    out, rest = [], path
    while "{" in rest:
        start = rest.index("{")
        end = rest.index("}", start)
        out.append(rest[start + 1:end])
        rest = rest[end + 1:]
    return out


def _ok_schema(path):
    if path == "/ask":
        return "Envelope"
    if path.endswith("/ask") or "/probes/" in path:
        return "AskResponse"
    if path.endswith("/plans") or path.endswith("/plans/{plan_id}"):
        return "PlanList" if path.endswith("/plans") else "Plan"
    if path.endswith("/audit") or path.endswith("/refusals"):
        return "AuditList"
    if path.endswith("/menu") or path.endswith("/capabilities"):
        return "MenuList"
    if path.endswith("/instances"):
        return "InstanceList"
    if path.endswith("/metrics"):
        return "Metrics"
    if path.endswith("/docs"):
        return "Html"
    if path.endswith("/openapi.json"):
        return "Spec"
    return "Generic"


def _post_body(path):
    if path.endswith("/ask"):
        return "AskRequest", {"question": "is the estate healthy",
                              "options": {"include_envelope": True,
                                          "max_results": 10,
                                          "timeout_ms": 30000}}
    if "/probes/" in path:
        return "ProbeRunRequest", {"args": {"host": "WS1"},
                                   "include_envelope": True}
    if path.endswith("/apply"):
        return "ApplyRequest", {"reason": "operator approved",
                                "dry_run": False}
    if path.endswith("/undo"):
        return "UndoRequest", {"reason": "rollback", "dry_run": False}
    return "Generic", {}


def _schemas():
    return {
        "Error": {
            "type": "object",
            "required": ["ok", "code", "error", "request_id"],
            "properties": {
                "ok": {"type": "boolean", "const": False},
                "code": {"type": "string", "enum": [
                    "unauthorized", "forbidden", "not_found",
                    "invalid_request", "refused", "conflict", "gone",
                    "timeout", "internal"]},
                "error": {"type": "string"},
                "request_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
        },
        "AskRequest": {
            "type": "object",
            "required": ["question"],
            "properties": {
                "question": {"type": "string", "minLength": 1},
                "options": {
                    "type": "object",
                    "properties": {
                        "include_envelope": {"type": "boolean",
                                             "default": False},
                        "max_results": {"type": "integer", "minimum": 1,
                                        "maximum": 1000},
                        "timeout_ms": {"type": "integer", "minimum": 1,
                                       "maximum": 3600000},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "ProbeRunRequest": {
            "type": "object",
            "properties": {
                "args": {"type": "object"},
                "include_envelope": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "AskResponse": {
            "type": "object",
            "required": ["ok", "request_id", "instance"],
            "properties": {
                "ok": {"type": "boolean"},
                "request_id": {"type": "string"},
                "instance": {"type": "string"},
                "probe": {"type": ["string", "null"]},
                "mode": {"type": ["string", "null"]},
                "refusal_reason": {"type": ["string", "null"]},
                "results": {},
                "envelope": {"$ref": "#/components/schemas/Envelope"},
                "gate_version": {"type": "integer"},
                "menu_version": {"type": "string"},
                "duration_ms": {"type": "number"},
                "code": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "Envelope": {
            "type": "object",
            "description": "The runtime envelope, returned exactly as produced.",
            "additionalProperties": True,
            "properties": {
                "ask_id": {"type": "string"},
                "ts": {"type": "number"},
                "normalized": {"type": "string"},
                "probe": {"type": ["string", "null"]},
                "menu_version": {"type": "string"},
                "mode": {"type": "string"},
                "results": {},
                "refusal_reason": {"type": ["string", "null"]},
                "verification": {"type": "object",
                                 "additionalProperties": True},
            },
        },
        "Plan": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "state": {"type": "string", "enum": ["proposed", "applied",
                                                     "undone"]},
                "action": {"type": "string"},
                "target": {"type": "string"},
                "reason": {"type": "string"},
                "operator": {"type": "string"},
                "created_ts": {"type": "number"},
                "applied_ts": {"type": ["number", "null"]},
                "undone_ts": {"type": ["number", "null"]},
                "stub": {"type": "boolean"},
                "events": {"type": "array", "items": {"type": "object"}},
            },
        },
        "PlanList": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "plans": {"type": "array", "items": _ref("Plan")},
                "next_cursor": {"type": ["string", "null"]},
            },
        },
        "ApplyRequest": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "UndoRequest": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "AuditRow": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "ts": {"type": "number"},
                "request_id": {"type": "string"},
                "remote": {"type": "string"},
                "key_id": {"type": "string"},
                "method": {"type": "string"},
                "path": {"type": "string"},
                "instance": {"type": "string"},
                "probe": {"type": ["string", "null"]},
                "plan_id": {"type": ["string", "null"]},
                "outcome": {"type": "string"},
                "status": {"type": "integer"},
                "duration_ms": {"type": "number"},
                "refusal_reason": {"type": ["string", "null"]},
                "question_sha256": {"type": ["string", "null"]},
                "prev": {"type": "string"},
                "hash": {"type": "string"},
            },
        },
        "AuditList": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "rows": {"type": "array", "items": _ref("AuditRow")},
                "next_cursor": {"type": ["string", "null"]},
            },
        },
        "MenuEntry": {"type": "object", "additionalProperties": True},
        "MenuList": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "menu": {"type": "array", "items": _ref("MenuEntry")},
            },
        },
        "Instance": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "probes": {"type": "integer"},
                "menu_version": {"type": "string"},
                "gate_version": {"type": "integer"},
                "model_loaded": {"type": "boolean"},
                "read_only": {"type": "boolean"},
            },
        },
        "InstanceList": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "instances": {"type": "array", "items": _ref("Instance")},
            },
        },
        "Metrics": {"type": "string"},
        "Html": {"type": "string"},
        "Spec": {"type": "object", "additionalProperties": True},
        "Generic": {"type": "object", "additionalProperties": True},
    }


def _examples():
    return {
        "ask_success": {"summary": "A routed answer",
                        "value": {"ok": True, "request_id": "req_01H...",
                                  "instance": "plasma-estate",
                                  "probe": "health", "mode": "deterministic",
                                  "refusal_reason": None,
                                  "results": [{"healthy": True}],
                                  "gate_version": 1,
                                  "menu_version": "272811b192f4",
                                  "duration_ms": 12}},
        "ask_refusal": {"summary": "A refusal with its reason",
                        "value": {"ok": False, "code": "refused",
                                  "request_id": "req_01H...",
                                  "instance": "plasma-estate",
                                  "refusal_reason": "no_probe_matches",
                                  "message": "no probe matches this question",
                                  "gate_version": 1,
                                  "menu_version": "272811b192f4"}},
        "probe_run": {"summary": "Typed probe execution",
                      "value": {"ok": True, "request_id": "req_01H...",
                                "instance": "plasma-estate", "probe": "health",
                                "mode": "probe", "refusal_reason": None,
                                "results": [{"healthy": True}],
                                "gate_version": 1,
                                "menu_version": "272811b192f4"}},
        "plan_list": {"summary": "Plans", "value": {
            "ok": True, "plans": [{"plan_id": "p_01H...", "state": "proposed",
                                   "action": "block_ip",
                                   "target": "1.2.3.4"}],
            "next_cursor": None}},
        "apply": {"summary": "Apply a proposed plan",
                  "value": {"ok": True, "plan_id": "p_01H...",
                            "state": "applied", "stub": True}},
        "undo": {"summary": "Undo an applied plan",
                 "value": {"ok": True, "plan_id": "p_01H...",
                           "state": "undone", "stub": True}},
        "audit_row": {"summary": "An audit row", "value": {
            "event_id": "evt_01H...", "ts": 1790000000.0,
            "request_id": "req_01H...", "remote": "10.0.0.5",
            "key_id": "9f2c1a7b3d40", "method": "POST",
            "path": "/v1/instances/plasma-estate/ask",
            "instance": "plasma-estate", "probe": "health", "plan_id": None,
            "outcome": "answered", "status": 200, "duration_ms": 12,
            "refusal_reason": None, "question_sha256": "5d4f...",
            "prev": "00", "hash": "ab12..."}},
        "unauthorized": {"summary": "Missing or wrong key", "value": {
            "ok": False, "code": "unauthorized", "error": "unauthorized",
            "request_id": "req_01H..."}},
        "forbidden": {"summary": "Read-only mode", "value": {
            "ok": False, "code": "forbidden",
            "error": "read-only mode refuses mutations",
            "request_id": "req_01H..."}},
        "not_found": {"summary": "Unknown instance/probe/plan", "value": {
            "ok": False, "code": "not_found", "error": "not found",
            "request_id": "req_01H..."}},
        "conflict": {"summary": "Illegal plan transition", "value": {
            "ok": False, "code": "conflict",
            "error": "plan is already applied", "request_id": "req_01H..."}},
        "gone": {"summary": "Expired plan", "value": {
            "ok": False, "code": "gone", "error": "plan expired",
            "request_id": "req_01H..."}},
        "too_large": {"summary": "Oversized body", "value": {
            "ok": False, "code": "invalid_request",
            "error": "request body too large", "request_id": "req_01H..."}},
        "timeout": {"summary": "Call timed out", "value": {
            "ok": False, "code": "timeout", "error": "call timed out",
            "request_id": "req_01H..."}},
    }
