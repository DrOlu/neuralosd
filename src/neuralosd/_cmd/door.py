"""`neuralosd door` — reach deployed instances from the host.

A deployed instance serves INSIDE a microVM. The door is how the host reaches
it, and there are two kinds:

  native    `msb create -p BIND:HOST:GUEST` forwards at CREATE time. This is
            the default `neuralosd deploy` path and needs nothing afterwards.

  relay     `msb modify` cannot add forwards to an already-created sandbox. For
            those, `door proxy` bridges the gap: it accepts TCP on the host and,
            per connection, runs a tiny pipe inside the VM that connects stdio
            to the inner port. One relay process per connection — fine for
            interactive Q&A, not for sustained concurrency.

Both kinds are recorded in ~/.neuralosd/doors.json so `door list` can show the
fleet and `door stop` can take a door down.

SIBLING CALLS (VM → VM) are not wired in this release: each microVM sits in its
own network namespace and cannot address a sibling's inner port. The design, for
when it is needed: a small resident listener inside each VM on a fixed
intra-fleet port; a sibling call becomes a door-proxy hop THROUGH THE HOST (VM
relay -> host -> target VM relay), reusing exactly the machinery here. The host
is the only namespace that can see every door, so the host is the switch.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time

DOORS_FILE = os.path.join(os.path.expanduser("~/.neuralosd"), "doors.json")

_RELAY_SNIPPET = (
    "import socket, sys, threading\n"
    "up = socket.create_connection(('127.0.0.1', %d))\n"
    "def pump(src, dst):\n"
    "    try:\n"
    "        while True:\n"
    "            b = src.read(65536) if hasattr(src, 'read') else src.recv(65536)\n"
    "            if not b: break\n"
    "            (dst.write if hasattr(dst, 'write') else dst.send)(b)\n"
    "    except Exception: pass\n"
    "    try: dst.shutdown(socket.SHUT_WR)\n"
    "    except Exception: pass\n"
    "t = threading.Thread(target=pump, args=(sys.stdin.buffer, up), daemon=True)\n"
    "t.start()\n"
    "pump(up, sys.stdout.buffer)\n"
    "t.join(timeout=5)\n")


def _load():
    if os.path.isfile(DOORS_FILE):
        try:
            with open(DOORS_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return {}
    return {}


def _save(doors):
    os.makedirs(os.path.dirname(DOORS_FILE), exist_ok=True)
    tmp = DOORS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doors, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, DOORS_FILE)


def _forget(name):
    doors = _load()
    if name in doors:
        del doors[name]
        _save(doors)


# ── the boxlite request-scoped forwarder ───────────────────────────────────
#
# boxlite's exec pipe has TWO verified halves: argv in, stdout out. Its stdin
# convention is undocumented rust and does not survive introspection, so a
# transparent byte relay is not honestly available for this backend. Instead
# each accepted connection is treated as ONE HTTP request: read it fully,
# run a one-shot exec inside the box that replays it against the inner door,
# and copy the response back. Fine for Q&A; not a streaming proxy.
#
# MEASURED LIVE, and worse: `Boxlite.default()` takes an EXCLUSIVE lock on
# ~/.boxlite. A long-running relay process would block neuralosd deploy, ask
# and every other boxlite process for as long as it ran. So the LISTENER never
# imports boxlite: each connection spawns a SHORT-LIVED child (_FORWARD_CHILD)
# that opens the runtime, forwards the one request, and exits - the lock is
# held only for the duration of a single request.

# Runs INSIDE the box. argv[1] = base64 request. Talks to the inner door,
# prints the response base64-encoded (text-safe through the exec channel).
_INNER_PUMP = (
    "import socket, sys, base64\n"
    "raw = base64.b64decode(sys.argv[1])\n"
    "s = socket.create_connection((\'127.0.0.1\', {port}), timeout=60)\n"
    "s.sendall(raw)\n"
    "s.settimeout(5)\n"
    "resp = b\'\'\n"
    "try:\n"
    "    while True:\n"
    "        c = s.recv(65536)\n"
    "        if not c: break\n"
    "        resp += c\n"
    "except socket.timeout: pass\n"
    "print(base64.b64encode(resp).decode())\n")

# Runs ON THE HOST, one per connection. Opens the runtime, runs the inner
# pump through the box's exec channel, prints the response base64-encoded.
_FORWARD_CHILD = (
    "import sys, base64, asyncio\n"
    "name, req_b64, box_dir = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "async def main():\n"
    "    import boxlite\n"
    "    rt = boxlite.Boxlite.default()\n"
    "    box = await rt.get(name)\n"
    "    # space-free argv: the staged pump file takes the b64 request\n"
    "    ex = await box.exec(\'python3\', "
    "[box_dir + \'/_pump.py\', req_b64], timeout_secs=120)\n"
    "    lines = []\n"
    "    async for line in ex.stdout():\n"
    "        lines.append(line)\n"
    "    await ex.wait()\n"
    "    sys.stdout.write(\'\'.join(lines).strip())\n"
    "asyncio.run(main())\n")


def _rest_url():
    """The boxlite REST endpoint, if configured AND reachable.

    `boxlite serve` is the high-throughput path for boxlite doors: its exec
    API needs no runtime lock and no process spawn. Opt in with
    NEURALOSD_BOXLITE_URL (plus NEURALOSD_BOXLITE_API_KEY when the serve has
    an api-key). Unreachable or unconfigured -> None, and callers fall back
    to the short-lived child.
    """
    url = os.environ.get("NEURALOSD_BOXLITE_URL")
    if not url:
        return None
    import urllib.request
    try:
        req = urllib.request.Request(url.rstrip("/") + "/v1/boxes")
        key = os.environ.get("NEURALOSD_BOXLITE_API_KEY")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=3) as r:
            return url.rstrip("/") if r.status == 200 else None
    except Exception:                                 # noqa: BLE001
        return None


_INNER_ONE_SHOT = (
    "import sys, base64, socket\n"
    "raw = base64.b64decode(sys.argv[1])\n"
    "s = socket.create_connection((\'127.0.0.1\', {port}), timeout=60)\n"
    "s.sendall(raw)\n"
    "s.settimeout(5)\n"
    "resp = b\'\'\n"
    "try:\n"
    "    while True:\n"
    "        c = s.recv(65536)\n"
    "        if not c: break\n"
    "        resp += c\n"
    "except socket.timeout: pass\n"
    "print(base64.b64encode(resp).decode())\n")


def _rest_forward(rest_url: str, sandbox: str, vm_port: int,
                  request: bytes) -> bytes:
    """One REST exec inside the box replays the request at the inner door."""
    import base64 as _b64
    import urllib.request
    inner_code = _INNER_ONE_SHOT.format(port=vm_port)
    payload = base64.b64encode(request).decode("ascii")
    body = json.dumps({"command": inner_code,
                       "args": [payload]}).encode()
    req = urllib.request.Request(
        rest_url.rstrip("/") + f"/v1/boxes/{sandbox}/exec", data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if (key := os.environ.get(
                     "NEURALOSD_BOXLITE_API_KEY")) else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read().decode())
    stdout = None
    if isinstance(out, dict):
        stdout = out.get("stdout") or out.get("output") or out.get("result")
    if stdout is None and isinstance(out, str):
        stdout = out
    if stdout is None:
        raise RuntimeError(f"unrecognized REST exec response: {str(out)[:120]}")
    return _b64.b64decode(stdout.strip())



def _forward_request(sandbox: str, vm_port: int, request: bytes, box_dir: str = None) -> bytes:
    """Forward ONE buffered HTTP request through a SHORT-LIVED child process.

    The child imports boxlite, replays the request against the inner door via
    exec, prints the response base64-encoded, and exits - releasing the
    runtime lock. base64 in and out, because the SDK's stdout stream is text.
    """
    rest = _rest_url()
    if rest:
        return _rest_forward(rest, sandbox, vm_port, request)
    import base64 as _b64
    req_b64 = _b64.b64encode(request).decode("ascii")
    script = _FORWARD_CHILD.format(
        inner=_INNER_PUMP.format(port=vm_port))
    child = subprocess.Popen(
        [sys.executable, "-c", script, sandbox, req_b64],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    out, err = child.communicate(timeout=180)
    if child.returncode != 0:
        raise RuntimeError(
            f"forward child failed: {err.decode(errors='replace')[-200:]}")
    return _b64.b64decode(out.strip())


# ── the relay (Option B fallback) ──────────────────────────────────────────

def _spawn_relay(msb_bin, sandbox, vm_port, backend="msb"):
    """One relay process per accepted connection: host stdin/stdout <-> the
    inner port inside the VM, via `msb exec`."""
    if backend == "boxlite":
        return None          # request-scoped in-process forwarder, see handler
    return subprocess.Popen(
        [msb_bin, "exec", sandbox, "--", "python3", "-c",
         _RELAY_SNIPPET % vm_port],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)


def proxy(msb_bin: str, sandbox: str, host: str, host_port: int, vm_port: int,
          stop_flag=None, backend: str = "msb"):
    """Accept host TCP connections and relay each through the exec channel.

    Long-running. Blocks. `stop_flag` (a threading.Event) ends the listener.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, host_port))
    srv.listen(16)
    print(f"door proxy: {host}:{host_port} -> {sandbox}:{vm_port} "
          f"(exec relay, one process per connection)", file=sys.stderr)
    try:
        while True:
            if stop_flag is not None and stop_flag.is_set():
                break
            srv.settimeout(1.0)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            if backend == "boxlite":
                # per-connection CHILD: the listener holds no boxlite, so the
                # runtime lock is only taken for the life of one request
                def handle_boxlite(conn=conn, sb=sandbox, vp=vm_port):
                    try:
                        request = _read_http_request(conn)
                        if not request:
                            return
                        conn.sendall(_forward_request(
                            sb, vp, request, box_dir=f"/app/{sb}"))
                    except Exception as exc:          # noqa: BLE001
                        try:
                            conn.sendall(
                                b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
                                b"door relay: forward failed")
                        except Exception:             # noqa: BLE001
                            pass
                    finally:
                        try:
                            conn.close()
                        except Exception:             # noqa: BLE001
                            pass
                threading.Thread(target=handle_boxlite, daemon=True).start()
                continue

            def handle(conn=conn):
                relay = _spawn_relay(msb_bin, sandbox, vm_port, backend)
                stdin_w = relay.stdin
                stdout_r = relay.stdout

                def host_to_vm():
                    try:
                        while True:
                            b = conn.recv(65536)
                            if not b:
                                break
                            stdin_w.write(b)
                            stdin_w.flush()
                    except Exception:
                        pass
                    try:
                        stdin_w.close()
                    except Exception:
                        pass

                t = threading.Thread(target=host_to_vm, daemon=True)
                t.start()
                try:
                    while True:
                        b = stdout_r.read(65536)
                        if not b:
                            break
                        conn.sendall(b)
                except Exception:
                    pass
                try:
                    conn.close()
                    relay.terminate()
                except Exception:
                    pass
            threading.Thread(target=handle, daemon=True).start()
    finally:
        srv.close()


# ── the CLI ────────────────────────────────────────────────────────────────

def run(a):
    from ..backends.msb_backend import msb_binary

    cmd = getattr(a, "door_cmd", None) or "list"
    doors = _load()

    if cmd == "list":
        if not doors:
            print(f"no doors registered ({DOORS_FILE})")
            return 0
        print(f"{'NAME':<22} {'SANDBOX':<22} {'HOST PORT':>9}  {'VM PORT':>7}")
        for name, d in sorted(doors.items()):
            print(f"{name:<22} {d.get('sandbox', '?'):<22} "
                  f"{d.get('host_port', '?'):>9}  {d.get('vm_port', '?'):>7}")
        return 0

    if cmd == "stop":
        name = a.name
        info = doors.get(name)
        if info is None:
            print(f"error: no door named {name!r} "
                  f"(known: {', '.join(sorted(doors)) or 'none'})")
            return 1
        msb = msb_binary()
        if msb:
            subprocess.run([msb, "stop", info.get("sandbox", name)],
                           capture_output=True, text=True, timeout=60)
        _forget(name)
        print(f"door '{name}' stopped and forgotten")
        return 0

    if cmd == "proxy":
        backend = getattr(a, "backend", "msb") or "msb"
        msb = msb_binary()
        if backend == "msb" and not msb:
            print("error: msb CLI not found", file=sys.stderr)
            return 1
        vm_port = a.target or a.port
        doors[a.name] = {"sandbox": a.name, "host_port": a.port,
                         "vm_port": vm_port, "kind": "relay",
                         "backend": backend, "registered": time.time()}
        _save(doors)
        try:
            proxy(msb, a.name, a.host, a.port, vm_port, backend=backend)
        except KeyboardInterrupt:
            pass
        finally:
            _forget(a.name)
        return 0

    print(f"error: unknown door command {cmd!r}", file=sys.stderr)
    return 2
