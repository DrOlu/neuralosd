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


# ── the relay (Option B fallback) ──────────────────────────────────────────

def _spawn_relay(msb_bin, sandbox, vm_port):
    """One relay process per accepted connection: host stdin/stdout <-> the
    inner port inside the VM, via `msb exec`."""
    return subprocess.Popen(
        [msb_bin, "exec", sandbox, "--", "python3", "-c",
         _RELAY_SNIPPET % vm_port],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)


def proxy(msb_bin: str, sandbox: str, host: str, host_port: int, vm_port: int,
          stop_flag=None):
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
            def handle(conn=conn):
                relay = _spawn_relay(msb_bin, sandbox, vm_port)
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
        msb = msb_binary()
        if not msb:
            print("error: msb CLI not found", file=sys.stderr)
            return 1
        vm_port = a.target or a.port
        doors[a.name] = {"sandbox": a.name, "host_port": a.port,
                         "vm_port": vm_port, "kind": "relay",
                         "registered": time.time()}
        _save(doors)
        try:
            proxy(msb, a.name, a.host, a.port, vm_port)
        except KeyboardInterrupt:
            pass
        finally:
            _forget(a.name)
        return 0

    print(f"error: unknown door command {cmd!r}", file=sys.stderr)
    return 2
