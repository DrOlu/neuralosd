"""Out-of-process plugins — a JSON-RPC host over stdin/stdout.

A `runtime: "process"` plugin runs as its own process. The parent sends
newline-delimited JSON requests and reads newline-delimited JSON replies. The
process gets a scrubbed environment, a working directory inside the plugin and
a per-call timeout; a crash or timeout cannot take the server down.

Protocol (one JSON object per line, both directions):

    {"id": 1, "method": "init",   "params": {"config": {...}, "permissions": {...}}}
    {"id": 1, "result": {"probes": [{name, description, triggers, args}],
                         "providers": [{name}]}}
    {"id": 2, "method": "probe",  "params": {"probe": NAME, "args": {...}}}
    {"id": 2, "result": <probe result>}
    {"id": 3, "method": "select", "params": {"question": Q, "menu": [...]}}
    {"id": 3, "result": {"probe": NAME, "args": {...}} | null}
    {"id": 4, "method": "health"}
    {"id": 5, "method": "shutdown"}

A plugin that exits, writes a non-JSON line, or exceeds the timeout is marked
failed; calls then raise, which the router surfaces as an error/refusal rather
than a guess.
"""
import atexit
import json
import os
import re
import subprocess
import threading
from typing import Any, Dict, List, Optional

from ..probe import probe as probe_dec
from ..router import plugin_context
from .manifest import PluginError, PluginManifest
from .registry import PluginRecord

MAX_LINE = 4 << 20


def _slug(name: str) -> str:
    tail = name.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "_", tail.lower()).strip("_") or "plugin"


class ProcessPlugin:
    def __init__(self, manifest: PluginManifest,
                 config: Optional[Dict[str, Any]] = None):
        if not manifest.command:
            raise PluginError(f"{manifest.name}: process plugin has no command")
        self.manifest = manifest
        self.config = dict(config or {})
        self.record = PluginRecord(manifest=manifest)
        self._lock = threading.Lock()
        self._next_id = 1
        self._failed = None
        env = {"PATH": os.environ.get("PATH", ""),
               "PYTHONUNBUFFERED": "1", "LANG": os.environ.get("LANG", "C.UTF-8")}
        try:
            self.proc = subprocess.Popen(
                manifest.command, cwd=manifest.path or None, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as exc:
            raise PluginError(f"{manifest.name}: cannot start plugin: {exc}")
        init = self._request("init", {"config": self.config,
                                      "permissions": manifest.permissions},
                             timeout=30)
        if not isinstance(init, dict):
            raise PluginError(f"{manifest.name}: init returned no object")
        prefix = _slug(manifest.name)
        for spec in init.get("probes") or []:
            self._add_probe(spec, prefix)
        for spec in init.get("providers") or []:
            name = spec.get("name") if isinstance(spec, dict) else str(spec)
            if name:
                self.record.providers[name] = _ProcessProvider(self, name)
        atexit.register(self.shutdown)

    # -- transport --------------------------------------------------------
    def _request(self, method: str, params: Dict[str, Any],
                 timeout: float) -> Any:
        if self._failed:
            raise PluginError(self._failed)
        with self._lock:
            if self.proc.poll() is not None:
                self._failed = f"{self.manifest.name}: plugin exited"
                raise PluginError(self._failed)
            request_id = self._next_id
            self._next_id += 1
            line = json.dumps({"jsonrpc": "2.0", "id": request_id,
                               "method": method, "params": params},
                              ensure_ascii=False)
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._failed = f"{self.manifest.name}: write failed: {exc}"
                raise PluginError(self._failed)
            timer = threading.Timer(timeout, self._kill)
            timer.start()
            try:
                reply = self.proc.stdout.readline(MAX_LINE)
            finally:
                timer.cancel()
            if not reply:
                # The timer may have killed the process: report WHY.
                self._failed = self._failed or (
                    f"{self.manifest.name}: plugin closed stdout")
                raise PluginError(self._failed)
            try:
                data = json.loads(reply)
            except ValueError as exc:
                self._failed = (f"{self.manifest.name}: invalid JSON from "
                                f"plugin: {exc}")
                raise PluginError(self._failed)
            if data.get("error"):
                raise PluginError(
                    f"{self.manifest.name}: {data['error']}")
            return data.get("result")

    def _kill(self):
        self._failed = f"{self.manifest.name}: plugin call timed out"
        try:
            self.proc.kill()
        except OSError:
            pass

    # -- registration -----------------------------------------------------
    def _add_probe(self, spec: Dict[str, Any], prefix: str) -> None:
        if not isinstance(spec, dict) or not spec.get("name"):
            return
        name = str(spec["name"])
        if not name.startswith(prefix + "_"):
            name = f"{prefix}_{name}"
        args = spec.get("args") if isinstance(spec.get("args"), dict) else {}

        def call(**kwargs):
            result = self._request("probe", {"probe": spec["name"],
                                             "args": kwargs},
                                   timeout=self._timeout())
            return result

        decorated = probe_dec(
            description=str(spec.get("description") or
                            f"{self.manifest.name} probe"),
            triggers=list(spec.get("triggers") or [name]),
            args=args, name=name, tier="plugin:process")(call)
        decorated._plugin = self.manifest.name
        self.record.probes.append(decorated)

    def _timeout(self) -> float:
        value = (self.manifest.permissions or {}).get("timeout_seconds", 30)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return 30.0

    def health(self) -> Dict[str, Any]:
        try:
            return self._request("health", {}, timeout=5) or {"ok": True}
        except PluginError as exc:
            return {"ok": False, "error": str(exc)}

    def shutdown(self) -> None:
        try:
            self._request("shutdown", {}, timeout=5)
        except PluginError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except OSError:
                pass


class _ProcessProvider:
    def __init__(self, host: ProcessPlugin, name: str):
        self.host = host
        self.name = name

    def available(self) -> bool:
        return self.host._failed is None

    def select(self, question, menu, k=8):
        return self.host._request(
            "select", {"question": question, "menu": menu},
            timeout=self.host._timeout())

    def complete(self, messages, **opts):
        return self.host._request(
            "complete", {"messages": messages, "options": opts},
            timeout=self.host._timeout())
