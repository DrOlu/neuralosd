"""`neuralosd serve` — HTTP service for one instance."""
import sys

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def run(a):
    from ._common import load_instance
    from ..service import serve

    inst = load_instance(a.instance_dir, with_model=getattr(a, "model", False))
    if getattr(a, "strict", False):
        inst.router.strict_discards = True

    host = getattr(a, "host", "127.0.0.1") or "127.0.0.1"
    print(f"serving instance '{inst.name}' with {len(inst.probes)} probes "
          f"on http://{host}:{a.port}", file=sys.stderr)
    if host not in LOOPBACK:
        # This is not hypothetical: /ask exposes the data behind every probe,
        # and there is no authentication on it yet. Binding wide is a
        # deliberate act, so say so out loud rather than inheriting 0.0.0.0
        # from a default and calling it configured.
        print("WARNING: bound to a non-loopback address, and /ask has NO "
              "authentication. Anyone who can reach this port can read every "
              "table this instance points at. Put an authenticating proxy in "
              "front, or bind 127.0.0.1 and tunnel.", file=sys.stderr)
    serve(inst, port=a.port, host=host)


def run_multi(a):
    """Multi-instance serving: one process, many instances, path-routed."""
    from ..multi import MultiInstanceServer, serve_multi
    from ._common import load_instance

    d = os.path.abspath(a.instances_dir)
    if not os.path.isdir(d):
        raise SystemExit(f"error: instances dir not found: {d}")

    server = MultiInstanceServer(instances_dir=d)
    inst_dirs = [x for x in os.listdir(d)
                 if os.path.isdir(os.path.join(d, x))]
    print(f"multi-instance serving from {d}")
    print(f"  {len(inst_dirs)} instance dirs found:")
    for name in sorted(inst_dirs):
        print(f"    {name}")
    print(f"  instances lazy-load on first request")
    print(f"  door: http://{getattr(a, 'host', '127.0.0.1')}:{a.port}")

    if getattr(a, "strict", False):
        print("  strict refusal policy: ON")

    serve_multi(server, host=getattr(a, "host", "127.0.0.1"), port=a.port)
