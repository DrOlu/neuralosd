"""Client for the neuralosd sidecar — used when probes need host libraries.

Discovery order (first match wins):
  1. ``$NEURALOSD_SIDECAR``          explicit command (shell-style string)
  2. ``neuralosd-sidecar`` on PATH
  3. ``python3 -m neuralosd.sidecar`` (host interpreter with neuralosd)

The sidecar is a *long-lived* subprocess, started once per instance and
reused for every probe call.  All failures raise :class:`SidecarError` with a
message that says what to do next.
"""
import atexit
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time

DEFAULT_TIMEOUT = 180.0
_ENV_VAR = "NEURALOSD_SIDECAR"

# Live clients, closed at interpreter exit so we never leak a subprocess.
_LIVE = []


class SidecarError(RuntimeError):
    """Any sidecar failure (not found, died, timed out, or returned an error)."""


# Values of $NEURALOSD_SIDECAR that mean "do not use a sidecar at all".
# An explicit opt-out is needed for testing and for users who want to be sure
# everything runs in-process (e.g. to prove a probe needs no host libraries).
_DISABLED = {"", "0", "off", "false", "no", "none", "-"}


def _split_cmd(text, posix=None):
    """Split a command line into argv.

    On Windows shlex must run in NON-posix mode: posix mode treats backslash
    as an escape character and silently destroys paths —
    ``C:\\tools\\helper.exe`` became ``C:toolshelper.exe``.  Non-posix mode
    preserves backslashes but keeps surrounding quotes, so those are stripped
    afterwards.  (A path containing spaces must still be quoted by the user,
    exactly as it would be on a Windows command line.)
    """
    if posix is None:
        posix = os.name != "nt"
    try:
        parts = shlex.split(text, posix=posix)
    except (ValueError, TypeError):
        parts = [text]
    if not posix:
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'"
                 else p for p in parts]
    return [p for p in parts if p]


def sidecar_command():
    """Return the argv prefix that launches a sidecar, or None if none exists.

    ``$NEURALOSD_SIDECAR=off`` (also 0/false/no/none/-) disables the sidecar
    entirely, even when a helper is installed.
    """
    explicit = os.environ.get(_ENV_VAR)
    if explicit is not None:
        if explicit.strip().lower() in _DISABLED:
            return None
        parts = _split_cmd(explicit)
        if parts:
            return parts

    exe = shutil.which("neuralosd-sidecar")
    if exe:
        return [exe]

    for py in ("python3", "python"):
        p = shutil.which(py)
        if p:
            return [p, "-m", "neuralosd.sidecar"]
    return None


class SidecarClient:
    """A long-lived sidecar subprocess speaking JSON-RPC over stdio."""

    def __init__(self, instance_dir: str, command=None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.instance_dir = os.path.abspath(instance_dir)
        self.command = list(command) if command else sidecar_command()
        self.timeout = timeout
        self.proc = None
        self._q = queue.Queue()
        self._err = []
        self._id = 0
        self._lock = threading.Lock()
        self._menu = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self.proc is not None:
            return self
        if not self.command:
            raise SidecarError(
                "no sidecar command found — install it with "
                "'pip install neuralosd' (provides the neuralosd-sidecar "
                "command) or set $NEURALOSD_SIDECAR")

        cmd = list(self.command) + ["--instance-dir", self.instance_dir]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            raise SidecarError(f"could not launch sidecar {cmd!r}: {e}")

        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        _LIVE.append(self)

        # The first line is the ready/error handshake.
        try:
            self._handshake()
        except SidecarError:
            self.close()
            raise
        return self

    def _handshake(self):
        deadline = time.time() + self.timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise SidecarError(
                    f"sidecar did not become ready in {self.timeout:g}s"
                    + self._err_tail())
            try:
                obj = self._q.get(timeout=remain)
            except queue.Empty:
                raise SidecarError(
                    f"sidecar did not become ready in {self.timeout:g}s"
                    + self._err_tail())
            if obj.get("__eof__"):
                raise SidecarError("sidecar exited before it was ready"
                                   + self._err_tail())
            if obj.get("event") == "error":
                raise SidecarError(f"sidecar failed to load the instance: "
                                   f"{obj.get('error')}")
            if obj.get("event") == "ready":
                return

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(json.dumps({"id": 0,
                                             "method": "shutdown"}) + "\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream and stream.close()
                except OSError:
                    pass
            if self in _LIVE:
                _LIVE.remove(self)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # -- pump threads ------------------------------------------------------
    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (OSError, ValueError):
            pass
        self._q.put({"__eof__": True})

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self._err.append(line)
                    del self._err[:-20]
        except (OSError, ValueError):
            pass

    def _err_tail(self, n=3):
        tail = " | ".join(self._err[-n:])
        return f" — stderr: {tail}" if tail else ""

    # -- protocol ----------------------------------------------------------
    def request(self, method: str, params=None, timeout=None):
        if self.proc is None:
            self.start()
        limit = self.timeout if timeout is None else timeout

        with self._lock:
            self._id += 1
            rid = self._id
            payload = json.dumps({"id": rid, "method": method,
                                  "params": params or {}})
            try:
                self.proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
                self.proc.stdin.flush()                # type: ignore[union-attr]
            except (OSError, ValueError) as e:
                raise SidecarError(f"sidecar is not accepting requests "
                                   f"({type(e).__name__}){self._err_tail()}")

            deadline = time.time() + limit
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise SidecarError(
                        f"sidecar timed out after {limit:g}s on {method!r}")
                try:
                    obj = self._q.get(timeout=remain)
                except queue.Empty:
                    raise SidecarError(
                        f"sidecar timed out after {limit:g}s on {method!r}")
                if obj.get("__eof__"):
                    raise SidecarError("sidecar exited unexpectedly"
                                       + self._err_tail())
                if obj.get("id") != rid:
                    continue  # stray line (handshake echo, late reply)
                if obj.get("ok"):
                    return obj.get("result")
                raise SidecarError(obj.get("error") or "sidecar error")

    def call(self, probe: str, args: dict):
        return self.request("call", {"probe": probe, "args": args or {}})

    def menu(self):
        if self._menu is None:
            self._menu = self.request("menu")
        return self._menu

    def ping(self):
        return self.request("ping")


def _close_all():
    for c in list(_LIVE):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_close_all)
