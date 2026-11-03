"""Pi Durable driver (v2.7.0) — durable, resumable pi conversations.

Pi Durable (@earendil-works/pi-durable) is Earendil's durable harness:
storage-backed conversations that survive crashes, idempotent submissions,
forks, multi-surface steering. It is a Node LIBRARY, so neuralosd ships a
small bridge script (pi_durable_bridge.mjs) and drives it over line-JSON
stdin/stdout via this module.

Durability contract (what makes this different from the one-shot builder):
  - the conversation lives in <instance>/pi_durable.sqlite — a crash or a
    `--resume` re-attaches to the SAME conversation
  - submissions are exactly-once by requestId: a retried build reuses the
    original submission instead of asking twice

Everything injectable (`spawn` / `execute`) — the whole flow is
unit-testable with a fake bridge process and no Node at all.
"""
import json
import os

DEFAULT_MODEL = os.environ.get("NEURALOSD_PI_MODEL", "z-ai/glm-5.3-flash")
DEFAULT_PROVIDER = os.environ.get("NEURALOSD_PI_PROVIDER", "openrouter")


def bridge_script_path():
    return os.path.join(os.path.dirname(__file__), "pi_durable_bridge.mjs")


def node_available() -> bool:
    import shutil
    return shutil.which("node") is not None


def pi_durable_available() -> bool:
    """Node present AND the pi-durable package resolvable from here."""
    if not node_available():
        return False
    import subprocess
    try:
        r = subprocess.run(
            ["node", "-e",
             "import('@earendil-works/pi-durable').then(()=>"
             "console.log('ok')).catch(()=>process.exit(1))"],
            capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pi_durable_command(storage, model=DEFAULT_MODEL,
                       provider=DEFAULT_PROVIDER):
    return ["node", bridge_script_path(),
            "--storage", storage,
            "--model", model,
            "--provider", provider]


class DurableBuilder:
    """A line-JSON conversation with a Pi Durable bridge process.

    `spawn` returns an object with `handle(obj) -> dict` (the fake bridge in
    tests) or is None (spawn the real node bridge). The driver never
    assumes which — it only speaks the line-JSON protocol.
    """

    def __init__(self, storage="<memory>", model=DEFAULT_MODEL,
                 provider=DEFAULT_PROVIDER, spawn=None, cwd=None):
        self.storage_path = storage
        self.cwd = cwd
        self._spawn = spawn
        self._pending = {}          # requestId -> waiting response
        self._proc = None
        self._fake = None
        if spawn is None:
            self._proc = self._spawn_real(model, provider)
        else:
            self._fake = spawn()

    # -- transport ---------------------------------------------------------

    def _spawn_real(self, model, provider):
        import subprocess
        argv = pi_durable_command(self.storage_path, model=model,
                                  provider=provider)
        try:
            return subprocess.Popen(
                argv, cwd=self.cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise SystemExit(
                "error: node is not installed — Pi Durable needs a "
                "JavaScript runtime (https://nodejs.org), plus "
                "npm i @earendil-works/pi-durable @earendil-works/pi-ai")

    def _roundtrip(self, obj, timeout=600):
        import time
        if self._fake is not None:
            return self._fake.handle(obj)
        proc = self._proc
        assert proc and proc.stdin and proc.stdout
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                return json.loads(line.decode())
            except ValueError:
                continue
        return {"op": "error", "message": "bridge timeout"}

    # -- protocol ----------------------------------------------------------

    def open(self, model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER,
             api_key=None):
        return self._roundtrip({"op": "open", "storage": self.storage_path,
                                "model": model, "provider": provider,
                                "api_key": api_key, "cwd": self.cwd})

    def submit(self, content, request_id=None):
        import hashlib
        rid = request_id or ("nosd-" + hashlib.sha256(
            (self.storage_path + content).encode()).hexdigest()[:12])
        resp = self._roundtrip({"op": "submit", "content": content,
                                "requestId": rid})
        if resp.get("op") == "error":
            raise SystemExit(f"error: pi durable submit: "
                             f"{resp.get('message', '')[:200]}")
        return resp

    def wait(self, request_id, timeout=600):
        import time
        deadline = time.time() + timeout
        last = {"op": "settled", "state": "unknown", "text": ""}
        while time.time() < deadline:
            last = self._roundtrip({"op": "wait", "requestId": request_id},
                                   timeout=60)
            if last.get("state") in ("completed", "aborted", "failed",
                                     "unknown"):
                break
            time.sleep(1)
        return last

    def status(self):
        return self._roundtrip({"op": "status"})

    def close(self):
        if self._fake is not None:
            return
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(b'{"op":"close"}\n')
                self._proc.stdin.flush()
                self._proc.wait(timeout=10)
        except Exception:                                    # noqa: BLE001
            pass
        if self._proc:
            self._proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def stable_request_id(instance_dir, question):
    import hashlib
    return "nosd-" + hashlib.sha256(
        (os.path.abspath(instance_dir) + "|" + question).encode()
    ).hexdigest()[:16]


def _spawn_fake_from(storage):
    """Test shim: callers pass spawn=lambda: <object with handle>."""
    raise NotImplementedError
