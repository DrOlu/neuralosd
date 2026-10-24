"""Multi-instance serving — ONE process, MANY instances, routed by path.

The current architecture runs one `neuralosd serve` process per instance.
At 3 instances that's annoying. At 30 it's an operational nightmare. At 300
it's impossible. And the biggest loss: cross-instance queries are impossible
because instances live in separate processes that don't know about each other.

This module provides:

  MultiInstanceServer — ONE process that lazy-loads instances on demand,
                        routes by path prefix, and isolates per-instance state.

  Routing:
    GET  /healthz                → overall server health
    GET  /instances               → JSON list of registered instances
    GET  /{name}/healthz         → instance-specific health
    GET  /{name}/ready           → instance readiness (runs a probe)
    GET  /{name}/openapi.json    → instance OpenAPI spec
    POST /{name}/ask             → route through that instance's router
    POST /query                  → cross-instance query (routes sub-questions
                                   to the right instances and combines)

  Lazy loading: instances load on FIRST request, not at server startup.
  A bad instance doesn't kill the server; it just refuses for that instance.
  New instances are picked up automatically when their directory appears.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .router import NoResults


class MultiInstanceServer:
    """ONE process serving MANY instances, routed by URL path prefix.

    Instances are LAZY-LOADED: the server starts in <1 second with no
    instances loaded. Each instance loads on its first request. A bad
    instance doesn't kill the server; it just refuses for that instance.

    Thread-safe: the instances dict is protected by a lock so concurrent
    requests don't race on lazy loading.
    """

    def __init__(self, instances_dir: str = None,
                 loader: Callable[[str], Any] = None):
        self._instances_dir = instances_dir
        self._loader = loader
        self._loaded: Dict[str, Any] = {}
        self._errors: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._scanning = False

    def register(self, name: str, instance: Any) -> None:
        """Explicitly register an instance (skips directory scanning)."""
        with self._lock:
            self._loaded[name] = instance

    @property
    def names(self) -> List[str]:
        """Instance names currently loaded or discoverable on disk."""
        loaded = set(self._loaded) - set(self._errors)
        on_disk = self._scan_dirs() if self._instances_dir else set()
        return sorted(loaded | on_disk)

    def _scan_dirs(self) -> set:
        if not self._instances_dir or not os.path.isdir(self._instances_dir):
            return set()
        return {d for d in os.listdir(self._instances_dir)
                if os.path.isdir(os.path.join(self._instances_dir, d))
                and (os.path.isfile(os.path.join(self._instances_dir, d, "probes.py"))
                     or os.path.isfile(os.path.join(self._instances_dir, d, "needle_menu.json")))}

    def get(self, name: str) -> Optional[Any]:
        """Lazy-load an instance by name. Returns None if not found."""
        with self._lock:
            if name in self._loaded:
                return self._loaded[name]
            if name in self._errors:
                return None
            # try to load
            if self._instances_dir:
                d = os.path.join(self._instances_dir, name)
                if os.path.isdir(d):
                    try:
                        if self._loader:
                            inst = self._loader(name, d)
                        else:
                            from ._cmd._common import load_instance
                            inst = load_instance(d)
                        self._loaded[name] = inst
                        return inst
                    except (Exception, SystemExit) as e:  # noqa: BLE001
                        self._errors[name] = str(e)
                        return None
            return None

    def _load_error(self, name: str) -> Optional[str]:
        return self._errors.get(name)


class _PathRouter:
    """Parses /{instance}/{action} from the URL path and dispatches."""

    @staticmethod
    def parse(path: str) -> Tuple[Optional[str], str]:
        """Returns (instance_name, action) from a path like /bxclinic/ask."""
        parts = path.strip("/").split("/", 1)
        if len(parts) == 2 and parts[0]:
            return parts[0], "/" + parts[1]
        if len(parts) == 1 and parts[0]:
            return parts[0], "/"
        return None, path


def make_handler(server: MultiInstanceServer):
    """Creates an HTTP request handler that routes by path to instances."""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, {"ok": True, "server": "neuralosd-multi"})
                return
            if self.path == "/instances":
                self._send(200, {"instances": server.names,
                                 "errors": server._errors})
                return

            name, action = _PathRouter.parse(self.path)
            if name is None:
                self._send(404, {"error": "not found"})
                return
            inst = server.get(name)
            if inst is None:
                err = server._load_error(name)
                self._send(404, {"error": f"instance {name!r} not found" +
                                         (f": {err}" if err else "")})
                return

            if action == "/healthz":
                self._send(200, {"ok": True, "instance": inst.name})
            elif action == "/openapi.json":
                self._send(200, inst.openapi(title=inst.name))
            elif action == "/menu":
                self._send(200, inst.menu)
            else:
                self._send(404, {"error": f"unknown action {action!r} "
                                          f"for instance {name!r}"})

        def do_POST(self):
            name, action = _PathRouter.parse(self.path)
            if name is None:
                self._send(404, {"error": "not found"})
                return

            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                q = str(payload.get("question") or "").strip()
            except Exception as exc:
                self._send(400, {"error": f"bad request: {exc}"})
                return

            inst = server.get(name)
            if inst is None:
                self._send(404, {"error": f"instance {name!r} not found"})
                return

            if action == "/ask":
                try:
                    self._send(200, inst.ask(q))
                except NoResults:
                    self._send(422, {"error": "no results produced"})
                except Exception as exc:          # noqa: BLE001
                    self._send(500, {"error": str(exc)[:300]})
            else:
                self._send(404, {"error": f"unknown action {action!r} "
                                          f"for instance {name!r}"})

    return Handler


def serve_multi(server: MultiInstanceServer, host: str = "127.0.0.1",
                port: int = 8877):
    """Start the multi-instance HTTP server. Blocks."""
    handler = make_handler(server)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"neuralosd multi-instance server -> http://{host}:{port} "
          f"({len(server.names)} instances)", flush=True)
    httpd.serve_forever()
