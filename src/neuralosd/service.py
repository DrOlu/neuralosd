from .router import NoResults

"""Minimal HTTP service for an Instance: /healthz /ready /ask /openapi."""
import json
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


def serve(instance, port: int = 8877, host: str = "0.0.0.0"):
    inst = instance

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, {"ok": True, "instance": inst.name})
            elif self.path == "/ready":
                try:
                    r = inst.ask(inst.menu[0]["triggers"][0])
                    self._send(200, {"ok": True, "probe": r.get("probe")})
                except Exception as exc:
                    self._send(503, {"ok": False, "error": str(exc)[:200]})
            elif self.path == "/openapi.json":
                self._send(200, inst.openapi())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/ask":
                self._send(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                q = str(payload.get("question") or "").strip()
            except Exception as exc:
                self._send(400, {"error": f"bad request: {exc}"})
                return
            if not q:
                self._send(400, {"error": "empty question"})
                return
            try:
                self._send(200, inst.ask(q))
            except NoResults:
                self._send(422, {"error": "no results produced"})
            except Exception as exc:
                self._send(500, {"error": str(exc)[:300]})

    print(f"neuralOS instance '{inst.name}' -> http://{host}:{port} "
          f"(/healthz /ready /ask)", flush=True)
    _Server((host, port), Handler).serve_forever()
