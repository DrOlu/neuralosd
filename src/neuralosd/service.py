from .router import NoResults, refusal_body, audit_source

"""Minimal HTTP service for an Instance: /healthz /ready /ask /openapi.

Optional bearer-token auth: pass `auth_token` (or set $NEURALOSD_API_TOKEN)
and every endpoint except the liveness probe (/healthz) requires
`Authorization: Bearer <token>` or `X-API-Token: <token>`. /ready EXECUTES a
real probe, so it is authenticated too (it used to be documented as open).
This is the "front door with a lock" that makes binding non-loopback a
defensible act.
"""
import hmac
import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that never blocks on reverse-DNS.

    HTTPServer.server_bind() calls socket.getfqdn(host), which can hang for
    tens of seconds (or forever) on networks with a slow/unreachable DNS.
    We bind the socket and set server_name to the literal host instead.
    """
    daemon_threads = True
    allow_reuse_address = True

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


def serve(instance, port: int = 8877, host: str = "0.0.0.0",
          auth_token: str = None, instance_dir: str = None):
    inst = instance
    _role_cache = {}

    def _instance_for_role(role):
        return resolve_role_instance(role, inst, instance_dir, _role_cache)

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if not auth_token:
                return True
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer ") and _token_ok(
                    auth[len("Bearer "):], auth_token):
                return True
            return _token_ok(self.headers.get("X-API-Token"), auth_token)

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # normalize: "/healthz?lb=1" and "/healthz/" are still liveness
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            # /healthz stays open: liveness must not need credentials, and it
            # exposes nothing but the instance NAME. /ready EXECUTES a real
            # probe, so it requires auth whenever auth is enabled.
            if not self._authorized() and path != "/healthz":
                self._send(401, {"error": "unauthorized: pass Authorization: "
                                           "Bearer <token> or X-API-Token"})
                return
            if path == "/healthz":
                self._send(200, {"ok": True, "instance": inst.name})
            elif path == "/ready":
                try:
                    # the health tick runs a REAL probe — stamp the row so
                    # 44 watchdog ticks an hour never inflate the user ledger
                    # (they now land in the self-test file as "watchdog" rows
                    # when golden's redirect is active, and are filterable via
                    # sources=("user",) when they do not)
                    with audit_source("watchdog"):
                        r = inst.ask(inst.menu[0]["triggers"][0])
                    self._send(200, {"ok": True, "probe": r.get("probe")})
                except Exception as exc:
                    self._send(503, {"ok": False, "error": str(exc)[:200]})
            elif path == "/openapi.json":
                self._send(200, inst.openapi())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._authorized():
                self._send(401, {"error": "unauthorized: pass Authorization: "
                                           "Bearer <token> or X-API-Token"})
                return
            if path != "/ask":
                self._send(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
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
            role = self.headers.get("X-NeuralOS-Role") or None
            if role and not acl_roles(instance_dir):
                self._send(403, {"error": "no acl.json — roles are not "
                                          "configured on this instance"})
                return
            role_inst, err = _instance_for_role(role)
            if err:
                self._send(403, {"error": f"unknown role: {role}"})
                return
            try:
                # served over HTTP = a real (authenticated) caller, but stamp
                # it anyway: "serve" rows are distinguishable from direct
                # library asks and from mesh relays in the same ledger
                with audit_source("serve"):
                    answer = role_inst.ask(q)
                self._send(200, answer)
            except NoResults as exc:
                self._send(422, refusal_body(exc))
            except Exception as exc:
                self._send(500, {"error": str(exc)[:300]})

    print(f"neuralOS instance '{inst.name}' -> http://{host}:{port} "
          f"(/healthz /ready /ask)"
          + ("  [token auth ON]" if auth_token else "  [NO AUTH]"), flush=True)
    _Server((host, port), Handler).serve_forever()
