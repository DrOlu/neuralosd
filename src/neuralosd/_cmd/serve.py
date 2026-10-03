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
