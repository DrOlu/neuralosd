"""`neuralosd deploy` — deploy an instance into a sandbox backend.

A deployment is only "deployed" when a question asked through the HOST door
returns real data. Everything before that is staging, and any failure in
staging is a FAILED deploy — the one unforgivable output is
"deployed ... service on port N" followed by a dead port.

The msb pipeline, in order (each step verified before the next):

  R0  the host port is free            (a busy port is not ours to take)
  R1  sandbox exists?                  reuse only with --force, else fail loud
  R2  create with a NATIVE port forward  (msb create -p BIND:HOST:GUEST;
                                        create-time only — msb modify cannot
                                        add forwards later)
  R3  agent reachable (ping)           a booted VM with no agent cannot serve
  R4  pip install neuralosd            python:3.12-slim ships nothing
  R5  stage the instance dir + data    the bridge hardcodes the source path;
                                       SOURCE is rewritten to a sandbox-local
                                       copy so the data rides with the deploy
  R6  start `neuralosd serve`          the console script, not -m (no
                                       __main__ in the package)
  R7  self-test INSIDE the VM          ask a real question through localhost
  R8  self-test from the HOST          through the native forward

Only after R8 does it print "deployed", and the door is recorded in
~/.neuralosd/doors.json so `neuralosd door list` can find it.
"""
import json
import os
import re
import shutil
import socket
import sys
import time
from typing import Optional
import urllib.error
import urllib.request

DOORS_FILE = os.path.join(os.path.expanduser("~/.neuralosd"), "doors.json")
DATA_SUBDIR = "data"


# ── small, injectable primitives (tests stub these) ────────────────────────

def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _http_json(url: str, payload: Optional[dict] = None, timeout: int = 30):
    """GET (payload None) or POST url. Returns parsed JSON or raises."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _wait_port_free_of_others(host, port, timeout=5):
    """Wait until something is LISTENING on host:port (the forward is up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _doors():
    if os.path.isfile(DOORS_FILE):
        try:
            with open(DOORS_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}
    return {}


def _save_doors(doors):
    os.makedirs(os.path.dirname(DOORS_FILE), exist_ok=True)
    tmp = DOORS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doors, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, DOORS_FILE)


def _record_door(name, sandbox, host_port, vm_port):
    doors = _doors()
    doors[name] = {"sandbox": sandbox, "host_port": host_port,
                   "vm_port": vm_port, "registered": time.time()}
    _save_doors(doors)


# ── the SOURCE path trap ───────────────────────────────────────────────────

_SOURCE_RE = re.compile(r'^(SOURCE\s*=\s*["\'])(.+?)(["\'].*)$', re.M)


def bridge_source(instance_dir: str) -> Optional[str]:
    """The data path hardcoded in the generated bridge, if any."""
    p = os.path.join(instance_dir, "bridge.py")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        m = _SOURCE_RE.search(fh.read())
    return m.group(2) if m else None


def _stage_instance(backend, sandbox, instance_dir, vm_dir):
    """Copy the instance into the sandbox and fix the data-source path.

    The generated bridge hardcodes SOURCE at the HOST's absolute path. Rather
    than mirroring host paths inside the VM (fragile), SOURCE is rewritten on
    the HOST before the bridge is copied - the staged bridge is then
    self-contained and no in-VM edit has to survive an exec channel's quoting.
    """
    copied = []
    src_path = bridge_source(instance_dir)
    data_note = "no external data file"
    vm_data = None
    if src_path:
        if not os.path.isfile(src_path):
            raise RuntimeError(
                f"bridge SOURCE {src_path!r} does not exist on the host - "
                f"the deployed instance would have no data")
        vm_data = f"{vm_dir}/{DATA_SUBDIR}/{os.path.basename(src_path)}"

    for fname in sorted(os.listdir(instance_dir)):
        if not fname.endswith((".py", ".json", ".md")):
            continue                      # state files (cache/audit) stay local
        if fname.startswith("_") or fname == "__pycache__":
            continue
        src = os.path.join(instance_dir, fname)
        if not os.path.isfile(src):
            continue

        if fname == "bridge.py" and vm_data:
            with open(src, encoding="utf-8") as fh:
                text = fh.read()
            text = _SOURCE_RE.sub(
                lambda m: m.group(1) + vm_data + m.group(3), text)
            tmp = src + ".staged"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            src = tmp

        res = backend.cp(src, sandbox, f"{vm_dir}/{fname}")
        if fname.endswith(".staged"):
            os.unlink(src)
        if res.get("exit") != 0:
            raise RuntimeError(f"staging {fname} failed: {res.get('stdout')}"
                               f"{res.get('stderr')}")
        copied.append(fname)

    if vm_data:
        res = backend.cp(src_path, sandbox, vm_data)
        if res.get("exit") != 0:
            raise RuntimeError(f"staging the data file failed: "
                               f"{res.get('stdout')}{res.get('stderr')}")
        copied.append(os.path.basename(src_path))
        data_note = f"{src_path} -> {vm_data} (SOURCE rewritten on the host)"
    return copied, data_note


# ── the pipeline ───────────────────────────────────────────────────────────

def _deploy_msb(a, backend):
    bind = getattr(a, "bind", "127.0.0.1")
    host_port, vm_port = int(a.port), int(a.port)
    vm_dir = f"/app/{a.name}"
    instance_dir = os.path.abspath(a.instance_dir)

    # R1 — idempotency. `msb modify` cannot add port forwards, so a re-deploy
    # on a different port means recreating the sandbox. Never silently reuse.
    # The removal happens BEFORE the port check: the old sandbox's forward is
    # what holds the port, and checking first made --force dead on arrival.
    listed = backend._run(["list", "--format", "json"])
    exists = a.name in (listed.stdout or "")
    if exists and not a.force:
        raise SystemExit(
            f"error: sandbox '{a.name}' already exists. Re-run with --force "
            f"to remove and redeploy it (the sandbox is stateless: the "
            f"instance lives on the host).")
    if exists:
        print(f"  removing existing sandbox '{a.name}' (--force) ...")
        backend.remove(a.name)

    # R0 — the host door must be free. A busy port is not ours to take, and
    # printing "service on port N" over someone else's listener is how an
    # unrelated launchd job once got blamed for this product.
    if not _port_free(bind, host_port):
        raise SystemExit(
            f"error: {bind}:{host_port} is already in use on the host. "
            f"Choose another --port; existing doors: "
            f"`neuralosd door list`")

    # R2 — create WITH the native forward.
    print(f"  creating sandbox '{a.name}' with forward "
          f"{bind}:{host_port} -> vm:{vm_port} ...")
    res = backend.create(a.name, ports=[(bind, host_port, vm_port)])
    if res.get("exit") != 0:
        raise SystemExit(f"error: sandbox creation failed: {res}")

    # R3 — the agent must be reachable before anything can be staged.
    deadline = time.time() + 60
    while backend.ping(a.name) != 0:
        if time.time() > deadline:
            raise SystemExit("error: sandbox agent never became reachable "
                             "(R3) — deployment failed")
        time.sleep(2)
    print("  agent reachable")

    # R4 — install neuralosd inside (slim ships nothing).
    print("  installing neuralosd inside the sandbox ...")
    res = backend.exec(a.name, "pip", ["install", "-q", "neuralosd"]
                       + (["--extra-index-url", a.index_url] if
                          getattr(a, "index_url", None) else []))
    if res.get("exit") != 0:
        raise SystemExit(f"error: pip install inside the sandbox failed:\n"
                         f"{res.get('stderr') or res.get('stdout')}")
    chk = backend.exec(a.name, "python3", ["-c", "import neuralosd"])
    if chk.get("exit") != 0:
        raise SystemExit("error: neuralosd is not importable inside the "
                         "sandbox after install (R4)")
    for extra in [p for p in (a.packages or "").split(",") if p]:
        print(f"  installing extra: {extra} ...")
        res = backend.exec(a.name, "pip", ["install", "-q", extra])
        if res.get("exit") != 0:
            raise SystemExit(f"error: installing {extra} failed:\n"
                             f"{res.get('stderr') or res.get('stdout')}")

    # R5 — stage the instance + its data.
    print("  staging the instance ...")
    mk = backend.exec(a.name, "mkdir", ["-p", f"{vm_dir}/{DATA_SUBDIR}"])
    if mk.get("exit") != 0:
        raise SystemExit(f"error: could not create {vm_dir} in the sandbox: "
                         f"{mk.get('stderr') or mk.get('stdout')}")
    copied, data_note = _stage_instance(backend, a.name, instance_dir, vm_dir)
    print(f"    {len(copied)} file(s): {', '.join(copied)}")
    print(f"    data: {data_note}")
    listing = backend.exec(a.name, "ls", [vm_dir])
    if listing.get("exit") != 0 or "probes.py" not in (listing.get("stdout") or ""):
        raise SystemExit("error: staged instance verification failed (R5) — "
                         "probes.py is not in the sandbox")

    # R6 — serve, via the console script (-m does not exist in this package).
    print(f"  starting neuralosd serve on 0.0.0.0:{vm_port} ...")
    start = (f"cd {vm_dir} && nohup neuralosd serve --instance-dir {vm_dir} "
             f"--host 0.0.0.0 --port {vm_port} > /var/log/neuralosd-serve.log "
             f"2>&1 & sleep 2; echo started")
    backend.exec(a.name, "sh", ["-c", start])

    # R7 — self-test INSIDE the VM: a real ask through the inner door.
    print("  self-test inside the sandbox ...")
    inner = ("python3", "-c",
             "import json, urllib.request;"
             "d = json.dumps({'question': 'how many rows'}).encode();"
             "req = urllib.request.Request("
             "'http://127.0.0.1:%d/ask', data=d,"
             "headers={'Content-Type': 'application/json'});"
             "out = json.loads(urllib.request.urlopen(req, timeout=30).read());"
             "print('SELFTEST-OK' if out.get('results') else 'SELFTEST-EMPTY')"
             % vm_port)
    inner_ok = False
    deadline = time.time() + 60
    while time.time() < deadline and not inner_ok:
        res = backend.exec(a.name, inner[0], [inner[1], inner[2]])
        if "SELFTEST-OK" in (res.get("stdout") or ""):
            inner_ok = True
        elif "SELFTEST-EMPTY" in (res.get("stdout") or ""):
            break                            # serving, but the probe found nothing
        time.sleep(2)
    if not inner_ok:
        logs = backend.exec(a.name, "cat",
                            ["/var/log/neuralosd-serve.log"])
        raise SystemExit("error: the inner service did not answer a real ask "
                         "(R7). serve log:\n"
                         f"{(logs.get('stdout') or '')[-800:]}")
    print("    inner self-test: PASS")

    # R8 — the HOST door: the whole point of the exercise.
    print(f"  self-test from the host through the forward ...")
    host_ok = False
    answer = None
    deadline = time.time() + 30
    while time.time() < deadline and not host_ok:
        try:
            answer = _http_json(f"http://{bind}:{host_port}/ask",
                                {"question": "how many rows"}, timeout=10)
            host_ok = bool(answer.get("results"))
        except (urllib.error.URLError, socket.timeout, OSError,
                ValueError):
            time.sleep(2)
    if not host_ok:
        raise SystemExit(f"error: the host door {bind}:{host_port} is not "
                         f"reachable through the forward (R8) — deployment "
                         f"failed; the sandbox is left running for diagnosis "
                         f"(`msb exec {a.name} -- cat "
                         f"/var/log/neuralosd-serve.log`)")
    probe_name = (answer.get("probe") or "?")

    _record_door(a.name, a.name, host_port, vm_port)
    print(f"\n✓ deployed '{a.name}' ({a.backend}) — VERIFIED end to end")
    print(f"  host door : http://{bind}:{host_port}  "
          f"(ask answered by {probe_name})")
    print(f"  vm door   : {vm_port} inside the sandbox")
    print(f"  registered: {DOORS_FILE}")
    return 0


def run(a):
    from ..backends import get_backend, available_backends

    inst_dir = os.path.abspath(a.instance_dir)
    if not os.path.isfile(os.path.join(inst_dir, "probes.py")):
        raise SystemExit(f"error: {inst_dir} has no probes.py")

    avail = available_backends()
    if not avail.get(a.backend):
        raise SystemExit(
            f"error: backend '{a.backend}' is not available on this host.\n"
            f"  msb:     install the Microsandbox `msb` CLI\n"
            f"  available now: {[k for k, v in avail.items() if v]}")

    if a.backend == "msb":
        backend = get_backend("msb")
        sys.exit(_deploy_msb(a, backend))

    # Non-msb backends keep their existing contract (unchanged this release).
    backend = get_backend(a.backend)
    print(f"deploying '{a.name}' via {a.backend} ...", file=sys.stderr)

    import asyncio

    async def _go():
        if hasattr(backend, "create") and asyncio.iscoroutinefunction(
                backend.create):
            await backend.create(a.name)
        else:
            backend.create(a.name)
        start = (f"pip install -q neuralosd && cd /app && "
                 f"python -m neuralosd.cli serve --instance-dir /app "
                 f"--port {a.port}")
        if asyncio.iscoroutinefunction(backend.exec):
            return await backend.exec(a.name, start)
        return backend.exec(a.name, start)

    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"deploy failed: {e}")

    print(f"deployed '{a.name}' ({a.backend}); service on port {a.port}")
