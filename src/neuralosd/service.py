"""Minimal HTTP service for an Instance: a guarded agent surface.

Endpoints
---------
    GET  /healthz                 liveness (open, like before)
    GET  /ready                   executes a probe (authenticated)
    GET  /openapi.json            the typed spec
    GET  /tools                   the agent tool surface (same as MCP)
    POST /ask                     one question (sync, or `async: true`)
    GET  /runs/{id}               poll a long-running ask
    DELETE /runs/{id}             cancel a long-running ask
    GET  /audit?run_id=...        read audit rows for a run/agent/session
    POST /v1/confirm/{token}      complete a pending HITL confirmation

Identity and policy
-------------------
`X-Agent-Id` / `X-Session-Id` / `X-Run-Id` (or the same keys in the body)
are stamped on the audit row. When `acl.json` declares an `agents` block it
is an allow-list: an unknown agent gets 403. An agent's `role` selects a
role-scoped instance; `pii: false` lets that agent see fields the instance
normally masks; `budget` caps calls per window (429 + `Retry-After`).

Contracts
---------
A refusal is 422 with the envelope plus `refusal` (code, retryable,
suggested_action, candidates, missing). An async ask is 202 with a run id.
`Idempotency-Key` (header or body) makes a retry return the first result
instead of executing again. Answers carry `content_trust: untrusted` and
`provenance` (instance, probe, ask id, ts, run, menu version).

Optional bearer-token auth: pass `auth_token` (or set $NEURALOSD_API_TOKEN)
and every endpoint except /healthz requires `Authorization: Bearer <token>`
or `X-API-Token: <token>`. /ready EXECUTES a real probe, so it is
authenticated too.
"""
import hmac
import json
import collections
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agentic import (BudgetStore, IdempotencyStore, RunStore, SessionStore,
                      TraceStore, agent_tokens, effective_role, hash_token,
                      refusal_contract, resolve_agent)
from .hitl import ConfirmRequired, ConfirmStore
from .router import NoResults, audit_fields, audit_source, pii_policy


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that never blocks on reverse-DNS.

    HTTPServer.server_bind() calls socket.getfqdn(host), which can hang for
    tens of seconds (or forever) on networks with a slow/unreachable DNS.
    We bind the socket and set server_name to the literal host instead.

    A thread per connection is unbounded by default; a caller can open
    thousands of sockets and exhaust the process. `MAX_WORKERS` caps the
    live request threads and answers the overflow with a 503 instead of
    accepting work it cannot run.
    """
    daemon_threads = True
    allow_reuse_address = True
    MAX_WORKERS = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.MAX_WORKERS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
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

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _token_ok(provided: str, expected: str) -> bool:
    # constant-time compare: a plain == leaks token length byte by byte
    return hmac.compare_digest((provided or "").encode(),
                               (expected or "").encode())


def acl_roles(instance_dir):
    """The instance's acl.json role map, or {} when unconfigured/unreadable.

    One reader for both the resolver and the HTTP boundary: the boundary
    used to test an undefined `_acl_roles` name here (F6), so every request
    that carried X-NeuralOS-Role without an acl.json died with a NameError
    and a closed socket instead of the documented 403.
    """
    if not instance_dir:
        return {}
    acl_path = os.path.join(instance_dir, "acl.json")
    if not os.path.isfile(acl_path):
        return {}
    try:
        with open(acl_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return {}
    roles = data.get("roles") if isinstance(data, dict) else None
    return roles if isinstance(roles, dict) else {}


def resolve_role_instance(role, inst, instance_dir, cache=None):
    """RBAC (v2.8.0): resolve the instance a request's role should run on.

    No acl.json or no role -> the default instance (None error). A role in
    acl.json -> a role-scoped instance, built once and cached. An unknown
    role -> (None, error) so the caller returns 403.
    """
    roles = acl_roles(instance_dir)
    if not role:
        return inst, None
    if role not in roles:
        return None, "unknown role"
    cache = cache if cache is not None else {}
    if role not in cache:
        from ._cmd._common import load_instance
        cache[role] = load_instance(instance_dir, role=role)
    return cache[role], None


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def serve(instance, port: int = 8877, host: str = "127.0.0.1",
          auth_token: str = None, instance_dir: str = None):
    inst = instance
    instance_dir = (instance_dir
                    or getattr(inst, "state_dir", None)
                    or os.path.dirname(os.path.abspath(
                        getattr(inst, "audit_file", "ask_audit.jsonl"))))
    # Fail closed: wide binding without any credential exposes every probe's
    # data and every agent's policy. Per-agent tokens count as a credential.
    if host not in LOOPBACK and not auth_token and not agent_tokens(instance_dir):
        raise SystemExit(
            f"error: refusing to bind {host} without a credential — pass "
            f"--auth-token (or $NEURALOSD_API_TOKEN), configure per-agent "
            f"tokens in acl.json, or bind 127.0.0.1")

    _role_cache = {}
    _runs_store = RunStore()
    _idem = IdempotencyStore()
    _budgets = BudgetStore()
    _sessions = SessionStore(instance_dir)
    _traces = TraceStore(instance_dir)
    _confirms = ConfirmStore()

    def _router_of(obj):
        """serve() accepts an Instance or a bare Router (the tests use one)."""
        return getattr(obj, "router", obj)

    _router_of(inst).confirm_store = _confirms

    def _instance_for_role(role):
        target, err = resolve_role_instance(role, inst, instance_dir,
                                            _role_cache)
        if target is not None:
            _router_of(target).confirm_store = _confirms
        return target, err

    def _annotate(env, agent_id, session_id, run_id, menu_version):
        env = dict(env)
        env["_meta"] = {"instance": inst.name, "agent_id": agent_id,
                        "session_id": session_id, "run_id": run_id,
                        "menu_version": menu_version}
        env["content_trust"] = "untrusted"
        env["provenance"] = {"instance": inst.name,
                             "probe": env.get("probe"),
                             "ask_id": env.get("ask_id"),
                             "ts": env.get("ts"),
                             "run_id": run_id,
                             "menu_version": menu_version}
        if env.get("mode") == "refused":
            env["refusal"] = refusal_contract(env)
        return env

    def _trace(run_id, event):
        _traces.record(run_id, event)

    class Handler(BaseHTTPRequestHandler):
        # A client that opens a connection and stalls cannot hold a worker
        # thread forever (slowloris).
        timeout = 30

        def _presented_token(self) -> str:
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                return auth[len("Bearer "):]
            return self.headers.get("X-API-Token") or ""

        def _caller(self, claimed_agent=None):
            """Resolve the request's credentials to (agent_id, mode).

            Modes: ``open`` (no credential configured — loopback/trusted),
            ``operator`` (the global token; may act as any agent),
            ``agent`` (a per-agent token; the claimed id must match),
            ``missing`` / ``bad`` / ``mismatch`` (reject).
            """
            token = self._presented_token()
            configured = bool(auth_token) or bool(agent_tokens(instance_dir))
            if not token:
                return (None, "missing") if configured else (None, "open")
            if auth_token and _token_ok(token, auth_token):
                return claimed_agent, "operator"
            agent = agent_tokens(instance_dir).get(hash_token(token))
            if not agent:
                return None, "bad"
            if claimed_agent and claimed_agent != agent:
                return None, "mismatch"
            return agent, "agent"

        def _authorized(self) -> bool:
            _agent, mode = self._caller(
                self.headers.get("X-Agent-Id") or None)
            return mode in ("open", "operator", "agent")

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self, payload=None):
            payload = payload if isinstance(payload, dict) else {}

            def clean(value, limit=200):
                return (value.strip()[:limit] if isinstance(value, str)
                        and value.strip() else None)

            return (clean(self.headers.get("X-Agent-Id")
                          or payload.get("agent_id")),
                    clean(self.headers.get("X-Session-Id")
                          or payload.get("session_id")),
                    clean(self.headers.get("X-Run-Id")
                          or payload.get("run_id")))

        def do_GET(self):
            raw = self.path.split("?", 1)[0]
            path = raw.rstrip("/") or "/"
            caller_agent, caller_mode = self._caller(
                self.headers.get("X-Agent-Id") or None)
            if caller_mode not in ("open", "operator", "agent") \
                    and path != "/healthz":
                self._send(401, {"error": "unauthorized: pass Authorization: "
                                           "Bearer <token> or X-API-Token"})
                return
            if path == "/healthz":
                self._send(200, {"ok": True, "instance": inst.name})
            elif path == "/ready":
                try:
                    # the health tick runs a REAL probe — stamp the row so
                    # 44 watchdog ticks an hour never inflate the user ledger
                    with audit_source("watchdog"):
                        r = inst.ask(inst.menu[0]["triggers"][0])
                    self._send(200, {"ok": True, "probe": r.get("probe")})
                except Exception as exc:
                    self._send(503, {"ok": False, "error": str(exc)[:200]})
            elif path == "/openapi.json":
                self._send(200, inst.openapi())
            elif path == "/tools":
                from .mcp import tools
                self._send(200, {"tools": tools()})
            elif path == "/audit":
                # An agent token sees only its own ledger; the operator sees
                # all. Without this, every agent could read every other
                # agent's questions and results.
                rows = self._read_audit(
                    force_agent=(caller_agent
                                 if caller_mode == "agent" else None))
                self._send(200, {"rows": rows})
            elif path.startswith("/runs/"):
                run_id = path[len("/runs/"):]
                run = _runs_store.get(run_id)
                if (run is not None and caller_mode == "agent"
                        and run.get("meta", {}).get("agent_id")
                        != caller_agent):
                    run = None           # do not reveal another agent's run
                if run is None:
                    self._send(404, {"error": f"unknown run: {run_id}"})
                else:
                    self._send(200, run)
            else:
                self._send(404, {"error": "not found"})

        def _read_audit(self, force_agent=None):
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            want = {k: (v[0] if v else None)
                    for k, v in qs.items()
                    if k in ("run_id", "agent_id", "session_id", "probe")}
            if force_agent:
                want["agent_id"] = force_agent
            try:
                limit = int((qs.get("limit") or ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            limit = max(1, min(limit, 1000))
            # A bare Router may write somewhere other than the default name
            # (the tests do); read the ledger it actually uses.
            path = (getattr(_router_of(inst), "audit_file", None)
                    or os.path.join(instance_dir, "ask_audit.jsonl"))
            # Stream the ledger, keeping only the newest `limit` matches: a
            # multi-GB audit must not be loaded into memory to answer one query.
            rows = collections.deque(maxlen=limit)
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and all(
                                row.get(k) == v for k, v in want.items()):
                            rows.append(row)
            except OSError:
                return []
            return list(rows)

        def do_POST(self):
            raw_path = self.path.split("?", 1)[0]
            path = raw_path.rstrip("/") or "/"
            _hdr_agent, mode = self._caller(
                self.headers.get("X-Agent-Id") or None)
            if mode in ("missing", "bad"):
                self._send(401, {"error": "unauthorized: pass Authorization: "
                                           "Bearer <token> or X-API-Token"})
                return
            if mode == "mismatch":
                self._send(403, {"error": "forbidden: the token does not "
                                          "belong to the claimed agent",
                                 "code": "agent_mismatch"})
                return

            if path.startswith("/v1/confirm/"):
                token = raw_path[len("/v1/confirm/"):].strip("/")
                result = _confirms.confirm(token)
                if isinstance(result, dict) and result.get("error"):
                    self._send(404, result)
                else:
                    result = dict(result) if isinstance(result, dict) else {
                        "result": result}
                    result["content_trust"] = "untrusted"
                    self._send(200, result)
                return

            if path != "/ask":
                self._send(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                self._send(400, {"error": "bad request: invalid Content-Length"})
                return
            if n < 0:
                self._send(400, {"error": "bad request: invalid Content-Length"})
                return
            if n > 1_000_000:
                # `read(-1)` would read the whole body; bound it by the
                # declared length so one request cannot exhaust memory.
                # Drain a bounded prefix first so the client can read this
                # 413 instead of seeing a connection reset mid-upload.
                remaining = min(n, 8_000_000)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self.close_connection = True
                self._send(413, {"error": "payload too large (max 1 MB)"})
                return
            try:
                raw = self.rfile.read(n) or b"{}"
                payload = json.loads(raw)
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": f"bad request: malformed JSON ({exc})"})
                return
            if not isinstance(payload, dict):
                self._send(400, {"error": "bad request: body must be a JSON "
                                          "object with a \"question\" key"})
                return
            q = payload.get("question")
            if q is None:
                q = ""
            if not isinstance(q, str):
                self._send(400, {"error": f"bad request: question must be a "
                                          f"string, got {type(q).__name__}"})
                return
            q = q.strip()
            if not q:
                self._send(400, {"error": "empty question"})
                return

            agent_id, session_id, run_id = self._identity(payload)
            caller_agent, caller_mode = self._caller(agent_id)
            if caller_mode in ("missing", "bad"):
                self._send(401, {"error": "unauthorized: a valid token is "
                                          "required"})
                return
            if caller_mode == "mismatch":
                self._send(403, {"error": "forbidden: the token does not "
                                          "belong to the claimed agent",
                                 "code": "agent_mismatch"})
                return
            if caller_mode == "agent":
                # The token IS the identity; the self-asserted header/body
                # cannot override it.
                agent_id = caller_agent
            idem_key = (self.headers.get("Idempotency-Key")
                        or payload.get("idempotency_key"))
            policy = resolve_agent(instance_dir, agent_id)
            if not policy["allowed"]:
                self._send(403, {"error": f"agent {agent_id!r} is not allowed "
                                          f"on this instance",
                                 "code": "policy_denied"})
                return
            # An agent token's role is the one in its ACL entry; the header
            # cannot escalate it. The operator token may still assume a role.
            role = effective_role(
                self.headers.get("X-NeuralOS-Role") or None,
                policy["role"], caller_mode)
            if role and not acl_roles(instance_dir):
                self._send(403, {"error": "no acl.json — roles are not "
                                          "configured on this instance"})
                return
            role_inst, err = _instance_for_role(role)
            if err:
                self._send(403, {"error": f"unknown role: {role}"})
                return

            ck = ((agent_id or "", session_id or "", idem_key)
                  if idem_key else None)
            if ck is not None and _idem.get(ck) is not None:
                replay = dict(_idem.get(ck))
                replay["_meta"] = {**replay.get("_meta", {}),
                                   "replayed": True}
                mode = replay.get("mode")
                code = 202 if mode == "pending_confirmation" else (
                    422 if mode == "refused" else 200)
                self._send(code, replay)
                return

            retry = _budgets.check(
                agent_id, policy["budget"]["calls"],
                policy["budget"]["window"])
            if retry is not None:
                self.send_response(429)
                self.send_header("Retry-After", str(max(1, int(retry))))
                body = json.dumps({
                    "error": "budget exhausted",
                    "code": "budget_exceeded",
                    "retry_after": max(1, int(retry)),
                    "limit": policy["budget"]["calls"],
                    "window": policy["budget"]["window"],
                }).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            def run_ask():
                previous = (_sessions.last(session_id)
                            if session_id else None)
                with audit_source("serve"), audit_fields(
                        agent_id=agent_id, session_id=session_id,
                        run_id=run_id, surface="serve"), \
                        pii_policy(policy["pii"]):
                    try:
                        env = role_inst.ask(q)
                    except NoResults as exc:
                        env = dict(exc.envelope)
                        env["mode"] = "refused"
                    except ConfirmRequired as exc:
                        env = {"mode": "pending_confirmation",
                               "confirm_token": exc.token,
                               "probe": exc.probe_name,
                               "reason": exc.reason,
                               "expires_in": _confirms.ttl}
                env = _annotate(env, agent_id, session_id, run_id,
                                _router_of(role_inst).menu_version)
                if session_id:
                    _sessions.remember(
                        session_id, q,
                        {"probe": env.get("probe"), "mode": env.get("mode"),
                         "ask_id": env.get("ask_id"),
                         "agent_id": agent_id})
                    env["_meta"]["session"] = {
                        "turn": _sessions.turns(session_id),
                        "previous": (previous or {}).get("text")}
                _trace(run_id or env.get("ask_id"), {
                    "tool": "ask", "agent_id": agent_id,
                    "session_id": session_id,
                    "probe": env.get("probe"), "mode": env.get("mode"),
                    "ask_id": env.get("ask_id"),
                    "refusal_reason": env.get("refusal_reason")})
                return env

            if payload.get("async"):
                rid = _runs_store.submit(
                    run_ask, meta={"question": q, "agent_id": agent_id})
                _trace(rid, {"tool": "ask", "status": "submitted",
                             "agent_id": agent_id,
                             "session_id": session_id})
                self._send(202, {"run_id": rid, "status": "queued"})
                return

            try:
                env = run_ask()
            except Exception as exc:  # a surface error, not a protocol crash
                self._send(500, {"error": str(exc)[:300]})
                return
            if ck is not None:
                _idem.put(ck, dict(env))
            code = 200
            if env.get("mode") == "refused":
                code = 422
            elif env.get("mode") == "pending_confirmation":
                code = 202
            self._send(code, env)

        def do_DELETE(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._authorized():
                self._send(401, {"error": "unauthorized: pass Authorization: "
                                           "Bearer <token> or X-API-Token"})
                return
            if path.startswith("/runs/"):
                run_id = path[len("/runs/"):]
                if _runs_store.cancel(run_id):
                    self._send(200, {"run_id": run_id, "cancelled": True})
                else:
                    self._send(404, {"error": f"unknown run: {run_id}"})
            else:
                self._send(404, {"error": "not found"})

    print(f"neuralOS instance '{inst.name}' -> http://{host}:{port} "
          f"(/healthz /ready /ask /tools /runs /audit /v1/confirm)"
          + ("  [token auth ON]" if auth_token else "  [NO AUTH]"), flush=True)
    _Server((host, port), Handler).serve_forever()
