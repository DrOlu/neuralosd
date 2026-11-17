"""First-class HTTP API for neuralOSd.

A small stdlib server (no new runtime dependency) that exposes the instance
surface over `/v1`, preserving the refusal gate, the signed envelope and the
plan/apply/undo gate. The model routes; it never applies an action.

The route table lives in :mod:`neuralosd.openapi_api`; the server dispatches
on it and the OpenAPI document is generated from it, so a path cannot exist
in one and not the other.
"""
import collections
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import __version__
from .actuate import _plan_state, _read_journal, actuate
from .hitl import ConfirmRequired, ConfirmStore
from .openapi_api import ROUTES, build_spec
from .router import (GATE_VERSION, NoResults, REFUSAL_REASONS, audit_fields,
                     audit_source, jsonable, mask_pii)

MAX_BODY = 1 << 20          # 1 MiB
MAX_OUTPUT = 8 << 20        # 8 MiB
DEFAULT_API_KEY = "neuralosd"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class ApiState:
    """Per-instance state: the loaded instance, caches, locks and audit."""

    def __init__(self, name, path, inst):
        self.name = name
        self.path = os.path.abspath(path)
        self.inst = inst
        self.lock = threading.RLock()
        self.audit_lock = threading.Lock()
        self.requests = collections.OrderedDict()
        self.idem = collections.OrderedDict()
        self.audit_path = os.path.join(self.path, "audit.jsonl")
        self.confirm = ConfirmStore()
        _router = getattr(inst, "router", inst)
        _router.confirm_store = self.confirm


class _HttpError(Exception):
    def __init__(self, status, code, message, extra=None):
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


def _match(template, path):
    """Match a path template; return params or None."""
    t = template.strip("/").split("/")
    p = path.strip("/").split("/")
    if len(t) != len(p) and not (template == "/healthz"):
        # the root templates have no variables; exact segment count is right
        return None
    if len(t) != len(p):
        return None
    params = {}
    for want, got in zip(t, p):
        if want.startswith("{") and want.endswith("}"):
            params[want[1:-1]] = got
        elif want != got:
            return None
    return params


def _int(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _validate_args(meta, args):
    errors = []
    declared = meta.args or {}
    for name in args:
        if name not in declared:
            errors.append(f"unknown argument {name!r}")
    for name, spec in declared.items():
        required = spec.get("required", True)
        if name not in args:
            if required:
                errors.append(f"missing required argument {name!r}")
            continue
        value = args[name]
        kind = spec.get("type")
        if kind in ("integer", "int"):
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{name!r} must be an integer")
                continue
            if "min" in spec and value < spec["min"]:
                errors.append(f"{name!r} below minimum {spec['min']}")
            if "max" in spec and value > spec["max"]:
                errors.append(f"{name!r} above maximum {spec['max']}")
        elif kind in ("number", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{name!r} must be a number")
                continue
        elif kind in ("enum", "string"):
            values = spec.get("values") or spec.get("enum")
            if values is not None and value not in values:
                errors.append(f"{name!r} must be one of {sorted(map(str, values))}")
            elif kind == "string" and not isinstance(value, str):
                errors.append(f"{name!r} must be a string")
        pattern = spec.get("pattern")
        if pattern and isinstance(value, str):
            try:
                if not re.fullmatch(pattern, value):
                    errors.append(f"{name!r} does not match the pattern")
            except re.error:
                pass
    return errors


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    MAX_WORKERS = 64

    def __init__(self, addr, handler, api, ssl_context=None):
        self.api = api
        self._slots = threading.BoundedSemaphore(self.MAX_WORKERS)
        super().__init__(addr, handler)
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket,
                                                  server_side=True)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Api:
    def __init__(self, states, api_keys, read_only=False, timeout=300,
                 max_body=MAX_BODY, max_output=MAX_OUTPUT, audit=True,
                 plan_ttl=86400, quiet=False, host="127.0.0.1", port=9998):
        self.states = {s.name: s for s in states}
        self.api_keys = list(dict.fromkeys(k for k in api_keys if k))
        self.read_only = read_only
        self.timeout = max(1, int(timeout))
        self.max_body = max(1, int(max_body))
        self.max_output = max(1, int(max_output))
        self.audit_enabled = bool(audit)
        self.plan_ttl = max(0, int(plan_ttl))
        self.quiet = quiet
        self.host = host
        self.port = port
        self.started = time.time()

    # -- helpers --------------------------------------------------------
    def state(self, name):
        st = self.states.get(name)
        if st is None:
            raise _HttpError(404, "not_found", f"unknown instance: {name}")
        return st

    def primary(self):
        if not self.states:
            raise _HttpError(503, "internal", "no instance configured")
        return next(iter(self.states.values()))

    def key_id(self, key):
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class Handler(BaseHTTPRequestHandler):
    timeout = 310

    @property
    def api(self):
        return self.server.api

    # -- transport ------------------------------------------------------
    def _send(self, status, payload, request_id, content_type="application/json",
              extra=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode()
        if len(body) > self.api.max_output:
            status = 500
            body = json.dumps({
                "ok": False, "code": "internal",
                "error": "response exceeds the output cap",
                "request_id": request_id}).encode()
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", request_id)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message, request_id):
        self._send(status, {"ok": False, "code": code, "error": message,
                            "request_id": request_id}, request_id)

    def _request_id(self):
        supplied = self.headers.get("X-Request-Id")
        if supplied and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", supplied):
            return supplied
        return "req_" + uuid.uuid4().hex[:20]

    def _presented_key(self):
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
        return self.headers.get("X-API-Key") or ""

    def _authenticate(self):
        key = self._presented_key()
        if not key:
            raise _HttpError(401, "unauthorized",
                             "missing bearer token or X-API-Key")
        for expected in self.api.api_keys:
            if hmac.compare_digest(key.encode(), expected.encode()):
                self.key_id = self.api.key_id(key)
                return
        raise _HttpError(401, "unauthorized", "invalid API key")

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise _HttpError(400, "invalid_request", "invalid Content-Length")
        if n < 0:
            raise _HttpError(400, "invalid_request", "invalid Content-Length")
        if n > self.api.max_body:
            remaining = min(n, 8 << 20)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            raise _HttpError(413, "invalid_request", "request body too large")
        raw = self.rfile.read(n) or b"{}"
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise _HttpError(400, "invalid_request",
                             f"malformed JSON: {exc}")
        if not isinstance(payload, dict):
            raise _HttpError(400, "invalid_request",
                             "body must be a JSON object")
        return payload

    def _call(self, fn, timeout):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise _HttpError(504, "timeout", "call timed out")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # -- dispatch -------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0] or "/"
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        request_id = self._request_id()
        start = time.time()
        opened = {"key_id": None, "probe": None, "plan_id": None,
                  "outcome": "error", "refusal_reason": None,
                  "question_sha256": None, "instance": None}
        meta = {}
        try:
            template = None
            params = None
            for m, t, _tag, _summary in ROUTES:
                if m != method:
                    continue
                candidate = _match(t, path)
                if candidate is not None:
                    template, params = t, candidate
                    break
            if template is None:
                raise _HttpError(404, "not_found", "not found")
            open_paths = {"/healthz", "/openapi.json", "/docs"}
            if template not in open_paths:
                self._authenticate()
                opened["key_id"] = self.key_id
            handler = self._ops().get((method, template))
            if handler is None:
                raise _HttpError(404, "not_found", "not found")
            body = self._read_body() if method == "POST" else {}
            status, payload, meta = handler(params, body, query, request_id)
        except _HttpError as exc:
            status, payload = exc.status, {
                "ok": False, "code": exc.code, "error": exc.message,
                "request_id": request_id, **exc.extra}
            meta = {}
        except Exception as exc:  # noqa: BLE001 — the surface must not crash
            status, payload = 500, {
                "ok": False, "code": "internal",
                "error": str(exc)[:300], "request_id": request_id}
            meta = {}
        duration = (time.time() - start) * 1000.0
        if isinstance(payload, dict) and not meta.get("no_request_id"):
            payload.setdefault("request_id", request_id)
            payload.pop("_audit", None)
        extra = {}
        # Audit BEFORE the response: the row must be durable by the time the
        # caller has the answer (a consumer may read the ledger immediately).
        # The block is exception-safe, so a logging failure cannot turn an
        # answer into an error.
        try:
            instance = meta.get("instance")
            if instance is None and isinstance(payload, dict):
                instance = payload.get("instance")
            state = self.api.states.get(instance) if instance else None
            if self.api.audit_enabled and state is not None and status != 401:
                row = {
                    "event_id": "evt_" + uuid.uuid4().hex[:16],
                    "ts": time.time(), "request_id": request_id,
                    "remote": self.client_address[0],
                    "key_id": opened["key_id"],
                    "method": method, "path": path,
                    "instance": state.name,
                    "probe": meta.get("probe"),
                    "plan_id": meta.get("plan_id"),
                    "outcome": ("refused" if meta.get("refusal_reason")
                                else "answered" if status < 400
                                else "error"),
                    "status": status,
                    "duration_ms": round(duration, 1),
                    "refusal_reason": meta.get("refusal_reason"),
                    "question_sha256": meta.get("question_sha256"),
                }
                self._audit_append(state, row)
        except Exception:
            pass
        self._send(status, payload, request_id,
                   content_type=meta.get("content_type", "application/json"),
                   extra=extra)

    def _audit_append(self, state, row):
        with state.audit_lock:
            prev = "00"
            try:
                with open(state.audit_path, encoding="utf-8") as fh:
                    last = None
                    for line in fh:
                        if line.strip():
                            last = line
                if last:
                    prev = json.loads(last).get("hash") or "00"
            except (OSError, ValueError):
                prev = "00"
            row = {**row, "prev": prev}
            canonical = json.dumps(row, sort_keys=True,
                                   separators=(",", ":")).encode()
            row["hash"] = hashlib.sha256(prev.encode() + canonical).hexdigest()
            try:
                with open(state.audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

    # -- operation table ------------------------------------------------
    def _ops(self):
        return {
            ("GET", "/healthz"): self._h_healthz,
            ("GET", "/openapi.json"): self._h_openapi,
            ("GET", "/docs"): self._h_docs,
            ("GET", "/v1/version"): self._h_version,
            ("GET", "/v1/instances"): self._h_instances,
            ("GET", "/v1/instances/{instance}"): self._h_instance,
            ("GET", "/v1/instances/{instance}/health"): self._h_health,
            ("GET", "/v1/instances/{instance}/capabilities"): self._h_caps,
            ("GET", "/v1/instances/{instance}/menu"): self._h_menu,
            ("GET", "/v1/instances/{instance}/menu/{probe}"): self._h_menu_probe,
            ("GET", "/v1/instances/{instance}/gate"): self._h_gate,
            ("GET", "/v1/instances/{instance}/model"): self._h_model,
            ("GET", "/v1/instances/{instance}/golden"): self._h_golden,
            ("GET", "/v1/instances/{instance}/config"): self._h_config,
            ("GET", "/v1/instances/{instance}/stats"): self._h_stats,
            ("POST", "/v1/instances/{instance}/ask"): self._h_ask,
            ("POST", "/v1/instances/{instance}/confirm/{token}"): self._h_confirm,
            ("POST", "/v1/instances/{instance}/probes/{probe}/run"): self._h_probe_run,
            ("GET", "/v1/instances/{instance}/requests/{request_id}"): self._h_request,
            ("GET", "/v1/instances/{instance}/requests/{request_id}/envelope"): self._h_request_env,
            ("GET", "/v1/instances/{instance}/plans"): self._h_plans,
            ("GET", "/v1/instances/{instance}/plans/{plan_id}"): self._h_plan,
            ("POST", "/v1/instances/{instance}/plans/{plan_id}/apply"): self._h_apply,
            ("POST", "/v1/instances/{instance}/plans/{plan_id}/undo"): self._h_undo,
            ("GET", "/v1/instances/{instance}/plans/{plan_id}/events"): self._h_events,
            ("GET", "/v1/instances/{instance}/audit"): self._h_audit,
            ("GET", "/v1/instances/{instance}/audit/{event_id}"): self._h_audit_event,
            ("GET", "/v1/instances/{instance}/audit/verify"): self._h_audit_verify,
            ("GET", "/v1/instances/{instance}/refusals"): self._h_refusals,
            ("POST", "/v1/instances/{instance}/reload"): self._h_reload,
            ("GET", "/v1/instances/{instance}/metrics"): self._h_metrics,
        }

    # -- discovery ------------------------------------------------------
    def _h_healthz(self, params, body, query, rid):
        return 200, {"ok": True, "request_id": rid}, {}

    def _h_openapi(self, params, body, query, rid):
        host = self.headers.get("Host") or f"{self.api.host}:{self.api.port}"
        # The document is a specification, not an API response: it must not
        # gain a request_id field (that invalidates the root schema).
        return (200, build_spec(host.split(":")[0], self.api.port),
                {"no_request_id": True})

    def _h_docs(self, params, body, query, rid):
        return 200, _docs_html(), {"content_type": "text/html"}

    def _h_version(self, params, body, query, rid):
        model = {"name": "needle-3", "params": 121_000_000,
                 "quantization": "2-bit"}
        try:
            import needle
            model["available"] = True
            model["version"] = getattr(needle, "__version__", None)
        except Exception:
            model["available"] = False
        loaded = any(getattr(getattr(s.inst, "router", s.inst),
                             "model_fallback", None)
                     for s in self.api.states.values())
        model["loaded"] = bool(loaded)
        return 200, {"ok": True, "version": __version__,
                     "gate_version": GATE_VERSION, "model": model,
                     "instances": {
                         s.name: getattr(s.inst, "router", s.inst).menu_version
                         for s in self.api.states.values()}}, {}

    def _h_instances(self, params, body, query, rid):
        return 200, {"ok": True, "instances": [
            self._instance_dict(s) for s in self.api.states.values()]}, {}

    def _instance_dict(self, s):
        router = getattr(s.inst, "router", s.inst)
        return {"name": s.name, "path": s.path,
                "probes": len(getattr(router, "metas", [])),
                "menu_version": router.menu_version,
                "gate_version": GATE_VERSION,
                "model_loaded": getattr(router, "model_fallback", None)
                is not None,
                "read_only": self.api.read_only}

    def _h_instance(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        return 200, {"ok": True, "instance": self._instance_dict(s)}, {}

    def _h_health(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        ready, error = False, None
        try:
            menu = router.menu()
            if menu and menu[0].get("triggers"):
                with audit_source("watchdog"):
                    self._call(lambda: s.inst.ask(menu[0]["triggers"][0],
                                                  use_cache=True), 5)
            ready = True
        except Exception as exc:
            error = str(exc)[:200]
        status = 200 if ready else 503
        return status, {"ok": ready, "ready": ready, "instance": s.name,
                        "probes": len(getattr(router, "metas", [])),
                        **({"error": error} if error else {})}, {}

    def _h_caps(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        return 200, {"ok": True, "capabilities": [
            {"name": m.name, "description": m.description,
             "triggers": list(m.triggers or [])}
            for m in router.metas]}, {}

    def _h_menu(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        return 200, {"ok": True, "menu": s.inst.menu,
                     "menu_version": s.inst.router.menu_version}, {}

    def _h_menu_probe(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        name = params["probe"]
        entry = next((m for m in s.inst.menu if m.get("name") == name), None)
        if entry is None:
            raise _HttpError(404, "not_found", f"unknown probe: {name}")
        return 200, {"ok": True, "probe": entry}, {}

    def _h_gate(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        return 200, {"ok": True, "gate_version": GATE_VERSION,
                     "reasons": sorted(REFUSAL_REASONS),
                     "rules": {
                         "floor": getattr(router, "floor", None),
                         "margin": getattr(router, "margin", None),
                         "min_question_coverage": getattr(
                             router, "min_question_coverage", None),
                         "refuse_disjunction": getattr(
                             router, "refuse_disjunction", None)}}, {}

    def _h_model(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        out = {"ok": True, "name": "needle-3", "params": 121_000_000,
               "quantization": "2-bit",
               "loaded": getattr(router, "model_fallback", None) is not None}
        try:
            import needle
            out["available"] = True
            out["version"] = getattr(needle, "__version__", None)
        except Exception:
            out["available"] = False
        return 200, out, {}

    def _h_golden(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        path = os.path.join(s.path, "golden.json")
        try:
            with open(path, encoding="utf-8") as fh:
                bank = json.load(fh)
        except (OSError, ValueError):
            bank = {}
        items = bank.get("items") if isinstance(bank, dict) else []
        cases = [{"q": it.get("q"), "expect_probe": it.get("expect_probe"),
                  "expect_refusal": bool(it.get("expect_refusal"))}
                 for it in items if isinstance(it, dict)][:1000]
        return 200, {"ok": True, "cases": cases,
                     "count": len(cases),
                     "last_verification": None}, {}

    def _h_config(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        return 200, {"ok": True, "name": s.name, "path": s.path,
                     "read_only": self.api.read_only,
                     "menu_version": router.menu_version,
                     "gate_version": GATE_VERSION,
                     "probes": len(router.metas),
                     "limits": {"timeout": self.api.timeout,
                                "max_body": self.api.max_body,
                                "max_output": self.api.max_output,
                                "plan_ttl": self.api.plan_ttl}}, {}

    def _h_stats(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        plans = len({p["plan_id"] for p in self._plans_data(s)})
        ask_rows = self._count_lines(getattr(router, "audit_file", ""))
        api_rows = self._count_lines(s.audit_path)
        return 200, {"ok": True, "instance": s.name,
                     "probes": len(router.metas), "plans": plans,
                     "audit_rows": api_rows, "ask_rows": ask_rows}, {}

    @staticmethod
    def _count_lines(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    # -- ask / probes ---------------------------------------------------
    def _h_ask(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            raise _HttpError(400, "invalid_request",
                             "question must be a non-empty string")
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise _HttpError(400, "invalid_request", "options must be an object")
        timeout = min(max(_int(options.get("timeout_ms"),
                              self.api.timeout * 1000, 1, 3_600_000) / 1000.0,
                          0.05), float(self.api.timeout))
        max_results = _int(options.get("max_results"), 1000, 1, 1000)
        include = bool(options.get("include_envelope", False))
        question_sha = hashlib.sha256(question.encode()).hexdigest()
        start = time.time()
        with audit_source("serve"), audit_fields(
                agent_id=f"api:{getattr(self, 'key_id', None)}",
                surface="api"):
            try:
                env = self._call(lambda: s.inst.ask(question), timeout)
            except NoResults as exc:
                env = dict(exc.envelope)
                env["mode"] = "refused"
            except ConfirmRequired as exc:
                raise _HttpError(
                    409, "conflict",
                    f"probe requires confirmation ({exc.reason})",
                    extra={"confirm_token": exc.token,
                           "probe": exc.probe_name})
        duration = (time.time() - start) * 1000.0
        refusal = env.get("refusal_reason")
        refused = env.get("mode") == "refused" or (
            env.get("results") is None and refusal)
        results = env.get("results")
        if isinstance(results, list):
            results = results[:max_results]
        gate_version = GATE_VERSION
        menu_version = env.get("menu_version") or s.inst.router.menu_version
        meta = {"instance": s.name, "probe": env.get("probe"),
                "refusal_reason": refusal, "question_sha256": question_sha}
        if refused:
            out = {"ok": False, "code": "refused", "request_id": rid,
                   "instance": s.name, "refusal_reason": refusal,
                   "message": env.get("error") or "refused",
                   "gate_version": gate_version,
                   "menu_version": menu_version}
            if include:
                out["envelope"] = env
            self._store(s, out, env)
            return 422, out, meta
        out = {"ok": True, "request_id": rid, "instance": s.name,
               "probe": env.get("probe"), "mode": env.get("mode"),
               "refusal_reason": None, "results": results,
               "gate_version": gate_version, "menu_version": menu_version,
               "duration_ms": round(duration, 1)}
        if include:
            out["envelope"] = env
        self._store(s, out, env)
        return 200, out, meta

    def _h_confirm(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        result = s.confirm.confirm(params["token"])
        if isinstance(result, dict) and result.get("error"):
            raise _HttpError(404, "not_found", result["error"])
        result = dict(result) if isinstance(result, dict) else {
            "result": result}
        result["ok"] = True
        result["request_id"] = rid
        return 200, result, {"instance": s.name}

    def _store(self, state, response, envelope):
        try:
            state.requests[response["request_id"]] = {
                "response": response, "envelope": envelope}
            state.requests.move_to_end(response["request_id"])
            while len(state.requests) > 500:
                state.requests.popitem(last=False)
        except Exception:
            pass

    def _h_probe_run(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        name = params["probe"]
        meta = next((m for m in s.inst.router.metas if m.name == name), None)
        if meta is None:
            raise _HttpError(404, "not_found", f"unknown probe: {name}")
        if getattr(meta, "confirm", False):
            raise _HttpError(409, "conflict",
                             "this probe requires confirmation; ask it "
                             "through /ask")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise _HttpError(400, "invalid_request", "args must be an object")
        errors = _validate_args(meta, args)
        if errors:
            raise _HttpError(400, "invalid_request", "; ".join(errors))
        if len(json.dumps(args, default=str)) > 100_000:
            raise _HttpError(400, "invalid_request", "args too large")
        include = bool(body.get("include_envelope", False))
        start = time.time()
        with audit_source("serve"), audit_fields(
                agent_id=f"api:{getattr(self, 'key_id', None)}",
                surface="api"):
            result = self._call(lambda: meta.function(**args),
                               float(self.api.timeout))
        duration = (time.time() - start) * 1000.0
        if isinstance(result, dict):
            results = [{**result, "_tool": name}]
        elif isinstance(result, list):
            results = result
        else:
            results = None
        results = jsonable(mask_pii(results))
        env = {"ask_id": uuid.uuid4().hex[:12], "ts": time.time(),
               "question": name, "normalized": name, "probe": name,
               "menu_version": s.inst.router.menu_version,
               "mode": "probe", "confidence": None,
               "latency_ms": int(duration), "results": results}
        s.inst.router._audit(env)
        out = {"ok": True, "request_id": rid, "instance": s.name,
               "probe": name, "mode": "probe", "refusal_reason": None,
               "results": results, "gate_version": GATE_VERSION,
               "menu_version": env["menu_version"],
               "duration_ms": round(duration, 1)}
        if include:
            out["envelope"] = env
        self._store(s, out, env)
        return 200, out, {"instance": s.name, "probe": name}

    def _h_request(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        rec = s.requests.get(params["request_id"])
        if rec is None:
            raise _HttpError(404, "not_found", "unknown request")
        return 200, {"ok": True, **rec["response"]}, {}

    def _h_request_env(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        rec = s.requests.get(params["request_id"])
        if rec is None:
            raise _HttpError(404, "not_found", "unknown request")
        return 200, {"ok": True, "request_id": params["request_id"],
                     "envelope": rec["envelope"]}, {}

    # -- plans ----------------------------------------------------------
    @staticmethod
    def _op(entry):
        return entry.get("op") or (
            "dry_run" if entry.get("dry_run")
            else "apply" if entry.get("applied")
            else "undo" if entry.get("undone") else None)

    def _plans_data(self, state):
        entries, error = _read_journal(state.path)
        if error:
            raise _HttpError(500, "internal", error)
        groups = collections.OrderedDict()
        for entry in entries:
            plan_id = entry.get("plan_id")
            if isinstance(plan_id, str):
                groups.setdefault(plan_id, []).append(entry)
        plans = []
        for plan_id, events in groups.items():
            st, identity = _plan_state(events, plan_id)
            if st is None:
                continue
            label = {"dry_run": "proposed", "applied": "applied",
                     "undone": "undone"}[st]

            def ts_of(op, reverse=False):
                seq = reversed(events) if reverse else events
                for entry in seq:
                    if self._op(entry) == op:
                        return entry.get("ts")
                return None

            plans.append({
                "plan_id": plan_id, "state": label,
                "action": identity.get("action") if identity else None,
                "target": identity.get("target") if identity else None,
                "reason": identity.get("reason") if identity else None,
                "operator": identity.get("operator") if identity else None,
                "created_ts": ts_of("dry_run"),
                "applied_ts": ts_of("apply", reverse=True),
                "undone_ts": ts_of("undo", reverse=True),
                "stub": bool(identity.get("stub")) if identity else False,
                "events": events,
            })
        plans.sort(key=lambda p: -(p["created_ts"] or 0))
        return plans

    def _plan_or_error(self, state, plan_id):
        plan = next((p for p in self._plans_data(state)
                     if p["plan_id"] == plan_id), None)
        if plan is None:
            raise _HttpError(404, "not_found", f"unknown plan: {plan_id}")
        if (plan["state"] == "proposed" and self.api.plan_ttl > 0
                and plan.get("created_ts")):
            if time.time() - plan["created_ts"] > self.api.plan_ttl:
                raise _HttpError(410, "gone", "plan expired")
        return plan

    def _h_plans(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        plans = self._plans_data(s)
        status = (query.get("status") or [None])[0]
        if status:
            plans = [p for p in plans if p["state"] == status]
        probe = (query.get("probe") or [None])[0]
        if probe:
            plans = [p for p in plans if p.get("probe") == probe]
        since = (query.get("since") or [None])[0]
        if since:
            try:
                cutoff = float(since)
                plans = [p for p in plans
                         if (p.get("created_ts") or 0) >= cutoff]
            except (TypeError, ValueError):
                raise _HttpError(400, "invalid_request", "invalid since")
        limit = _int((query.get("limit") or ["100"])[0], 100, 1, 1000)
        offset = _int((query.get("cursor") or ["0"])[0], 0, 0, 10_000_000)
        page = plans[offset:offset + limit]
        nxt = str(offset + limit) if offset + limit < len(plans) else None
        return 200, {"ok": True, "plans": page, "next_cursor": nxt}, {}

    def _h_plan(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        plan = self._plan_or_error(s, params["plan_id"])
        return 200, {"ok": True, "plan": plan}, {}

    def _h_events(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        plan = self._plan_or_error(s, params["plan_id"])
        return 200, {"ok": True, "plan_id": plan["plan_id"],
                     "events": plan["events"]}, {}

    def _require_mutable(self):
        if self.api.read_only:
            raise _HttpError(403, "forbidden",
                             "read-only mode refuses mutations")

    def _idem_key(self):
        key = self.headers.get("Idempotency-Key")
        if key and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            return key
        return None

    def _idem_replay(self, state, key):
        if not key or key not in state.idem:
            return None
        status, body = state.idem[key]
        return status, {**body, "replayed": True}

    def _idem_store(self, state, key, status, body):
        if not key:
            return
        state.idem[key] = (status, body)
        while len(state.idem) > 500:
            state.idem.popitem(last=False)

    def _h_apply(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        plan_id = params["plan_id"]
        reason = body.get("reason") or ""
        if not isinstance(reason, str):
            raise _HttpError(400, "invalid_request", "reason must be a string")
        dry_run = bool(body.get("dry_run", False))
        if not dry_run:
            # A dry-run is a read; only the transition is a mutation.
            self._require_mutable()
        key = self._idem_key()
        with s.lock:
            replay = self._idem_replay(s, key)
            if replay is not None:
                return replay[0], replay[1], {"instance": s.name,
                                              "plan_id": plan_id}
            plan = self._plan_or_error(s, plan_id)
            if plan["state"] != "proposed":
                raise _HttpError(409, "conflict",
                                 f"plan is {plan['state']}; apply requires a "
                                 f"proposed plan")
            if dry_run:
                out = {"ok": True, "plan_id": plan_id, "state": "proposed",
                       "dry_run": True, "would_apply": True}
                return 200, out, {"instance": s.name, "plan_id": plan_id}
            try:
                report = actuate(
                    plan["action"], plan["target"],
                    reason or plan.get("reason") or "operator approved",
                    f"api:{getattr(self, 'key_id', None) or 'unknown'}",
                    s.path, apply_plan=plan_id, side_effect=None, stub=True)
            except SystemExit as exc:
                message = str(exc)
                code = (404 if "no dry-run" in message
                        else 409)
                raise _HttpError(code, "not_found" if code == 404
                                 else "conflict", message)
            out = {"ok": True, "plan_id": plan_id, "state": "applied",
                   "stub": True, "report": report}
            self._idem_store(s, key, 200, out)
            return 200, out, {"instance": s.name, "plan_id": plan_id}

    def _h_undo(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        plan_id = params["plan_id"]
        reason = body.get("reason") or ""
        if not isinstance(reason, str):
            raise _HttpError(400, "invalid_request", "reason must be a string")
        dry_run = bool(body.get("dry_run", False))
        if not dry_run:
            self._require_mutable()
        key = self._idem_key()
        with s.lock:
            replay = self._idem_replay(s, key)
            if replay is not None:
                return replay[0], replay[1], {"instance": s.name,
                                              "plan_id": plan_id}
            plan = self._plan_or_error(s, plan_id)
            if plan["state"] != "applied":
                raise _HttpError(409, "conflict",
                                 f"plan is {plan['state']}; undo requires an "
                                 f"applied plan")
            if dry_run:
                out = {"ok": True, "plan_id": plan_id, "state": "applied",
                       "dry_run": True, "would_undo": True}
                return 200, out, {"instance": s.name, "plan_id": plan_id}
            try:
                report = actuate(
                    plan["action"], plan["target"],
                    reason or "rollback",
                    f"api:{getattr(self, 'key_id', None) or 'unknown'}",
                    s.path, undo_plan_id=plan_id, side_effect=None, stub=True)
            except SystemExit as exc:
                raise _HttpError(409, "conflict", str(exc))
            out = {"ok": True, "plan_id": plan_id, "state": "undone",
                   "stub": True, "report": report}
            self._idem_store(s, key, 200, out)
            return 200, out, {"instance": s.name, "plan_id": plan_id}

    # -- audit ----------------------------------------------------------
    def _read_audit_rows(self, state):
        rows = []
        try:
            with open(state.audit_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            return []
        return rows

    def _h_audit(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        rows = self._read_audit_rows(s)
        outcome = (query.get("outcome") or [None])[0]
        if outcome:
            rows = [r for r in rows if r.get("outcome") == outcome]
        since = (query.get("since") or [None])[0]
        if since:
            try:
                cutoff = float(since)
                rows = [r for r in rows if (r.get("ts") or 0) >= cutoff]
            except (TypeError, ValueError):
                raise _HttpError(400, "invalid_request", "invalid since")
        limit = _int((query.get("limit") or ["100"])[0], 100, 1, 1000)
        offset = _int((query.get("cursor") or ["0"])[0], 0, 0, 10_000_000)
        page = rows[offset:offset + limit]
        nxt = str(offset + limit) if offset + limit < len(rows) else None
        return 200, {"ok": True, "rows": page, "next_cursor": nxt}, {}

    def _h_audit_event(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        row = next((r for r in self._read_audit_rows(s)
                    if r.get("event_id") == params["event_id"]), None)
        if row is None:
            raise _HttpError(404, "not_found", "unknown audit event")
        return 200, {"ok": True, "row": row}, {}

    def _h_audit_verify(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        rows = self._read_audit_rows(s)
        prev = "00"
        for index, row in enumerate(rows):
            stored = row.get("hash")
            body_row = {k: v for k, v in row.items() if k != "hash"}
            canonical = json.dumps(body_row, sort_keys=True,
                                   separators=(",", ":")).encode()
            expected = hashlib.sha256(prev.encode() + canonical).hexdigest()
            if row.get("prev") != prev or stored != expected:
                return 200, {"ok": False, "chained": True,
                             "rows": len(rows), "broken_at": index}, {}
            prev = stored or "00"
        return 200, {"ok": True, "chained": True, "rows": len(rows)}, {}

    def _h_refusals(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        rows = []
        try:
            with open(router.audit_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if row.get("mode") == "refused" or row.get("refusal_reason"):
                        rows.append({
                            "ts": row.get("ts"),
                            "probe": row.get("probe"),
                            "refusal_reason": row.get("refusal_reason"),
                            "mode": row.get("mode"),
                            "question_sha256": hashlib.sha256(
                                str(row.get("question") or "").encode()
                            ).hexdigest() if row.get("question") else None,
                            "scores": row.get("scores") or [],
                        })
        except OSError:
            rows = []
        reason = (query.get("reason") or [None])[0]
        if reason:
            rows = [r for r in rows if r.get("refusal_reason") == reason]
        limit = _int((query.get("limit") or ["100"])[0], 100, 1, 1000)
        return 200, {"ok": True, "refusals": rows[-limit:]}, {}

    # -- admin ----------------------------------------------------------
    def _h_reload(self, params, body, query, rid):
        self._require_mutable()
        s = self.api.state(params["instance"])
        from ._cmd._common import load_instance
        with s.lock:
            old_router = getattr(s.inst, "router", s.inst)
            inst = load_instance(
                s.path, with_model=getattr(old_router, "model_fallback",
                                           None) is not None)
            router = getattr(inst, "router", inst)
            router.confirm_store = s.confirm
            s.inst = inst
        return 200, {"ok": True, "instance": s.name,
                     "menu_version": s.inst.router.menu_version}, {
            "instance": s.name}

    def _h_metrics(self, params, body, query, rid):
        s = self.api.state(params["instance"])
        router = getattr(s.inst, "router", s.inst)
        plans = len(self._plans_data(s))
        refusals = sum(1 for r in self._read_audit_rows(s)
                       if r.get("outcome") == "refused")
        lines = [
            f'neuralosd_probes{{instance="{s.name}"}} {len(router.metas)}',
            f'neuralosd_plans{{instance="{s.name}"}} {plans}',
            f'neuralosd_refusals{{instance="{s.name}"}} {refusals}',
            f'neuralosd_audit_rows{{instance="{s.name}"}} '
            f'{self._count_lines(s.audit_path)}',
            f'neuralosd_uptime_seconds {int(time.time() - self.api.started)}',
        ]
        return 200, "\n".join(lines) + "\n", {
            "content_type": "text/plain; version=0.0.4"}


def _docs_html():
    rows = "".join(
        f"<tr><td><code>{method}</code></td><td><code>{path}</code></td>"
        f"<td>{tag}</td><td>{summary}</td></tr>"
        for method, path, tag, summary in ROUTES)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>neuralOSd HTTP API</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;
max-width:1100px;color:#1f2328}}
h1{{margin-bottom:.2rem}} .sub{{color:#57606a;margin-top:0}}
table{{border-collapse:collapse;width:100%;margin-top:1.5rem}}
th,td{{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #d0d7de;
vertical-align:top}} th{{background:#f6f8fa}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}}
.badge{{display:inline-block;padding:.1rem .45rem;border-radius:6px;
background:#ddf4ff;color:#0969da;font-size:.8rem}}
</style></head><body>
<h1>neuralOSd HTTP API</h1>
<p class="sub">Version {__version__} · OpenAPI 3.1 ·
<span class="badge">authenticated</span> ·
<a href="/openapi.json">/openapi.json</a></p>
<p>Every answer comes from an allowlisted probe. A refusal is returned with
its reason, never translated into a guess. The model routes; it never applies
an action. Mutating plan endpoints are refused in <code>--read-only</code>
mode.</p>
<table><thead><tr><th>Method</th><th>Path</th><th>Tag</th>
<th>Purpose</th></tr></thead><tbody>{rows}</tbody></table>
<p class="sub">Authentication: <code>Authorization: Bearer &lt;key&gt;</code>
or <code>X-API-Key: &lt;key&gt;</code>. /healthz, /openapi.json and /docs are
open.</p>
</body></html>"""


def serve_api(instance_dirs, host="127.0.0.1", port=9998, api_keys=None,
              api_key_file=None, read_only=False, tls_cert=None, tls_key=None,
              timeout=300, max_body=MAX_BODY, max_output=MAX_OUTPUT,
              audit=True, plan_ttl=86400, quiet=False, with_model=False,
              strict=False, states=None):
    """Load the instances and serve the API. Blocks."""
    from ._cmd._common import load_instance

    names = set()
    if states is None:
        states = []
        for directory in instance_dirs:
            path = os.path.abspath(os.path.expanduser(directory))
            name = os.path.basename(path.rstrip("/")) or "instance"
            if name in names:
                raise SystemExit(
                    f"error: two instances share the name {name!r}; "
                    f"use distinct directory names")
            names.add(name)
            inst = load_instance(path, with_model=with_model)
            if strict:
                inst.router.strict_discards = True
            states.append(ApiState(name, path, inst))
    keys = list(api_keys or [])
    if api_key_file:
        try:
            with open(api_key_file, encoding="utf-8") as fh:
                keys.extend(line.strip() for line in fh if line.strip())
        except OSError as exc:
            raise SystemExit(f"error: cannot read --api-key-file: {exc}")
    if not keys:
        keys = [DEFAULT_API_KEY]
    if host not in LOOPBACK and keys == [DEFAULT_API_KEY]:
        raise SystemExit(
            "error: refusing to bind a non-loopback address with the default "
            "lab API key — pass --api-key/--api-key-file (or bind 127.0.0.1)")
    if bool(tls_cert) != bool(tls_key):
        raise SystemExit("error: --tls-cert and --tls-key must be used together")

    api = Api(states, keys, read_only=read_only, timeout=timeout,
              max_body=max_body, max_output=max_output, audit=audit,
              plan_ttl=plan_ttl, quiet=quiet, host=host, port=port)
    context = None
    if tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
    server = _Server((host, port), Handler, api, ssl_context=context)
    if not quiet:
        print(f"neuralOSd API {__version__} -> "
              f"{'https' if tls_cert else 'http'}://{host}:{port} "
              f"({len(states)} instance(s), "
              f"{'read-only' if read_only else 'mutable'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

