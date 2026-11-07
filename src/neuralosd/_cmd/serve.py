"""`neuralosd serve` — HTTP service for one instance."""
import os
import sys

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def run(a):
    from ._common import load_instance
    from ..service import serve

    inst = load_instance(a.instance_dir, with_model=getattr(a, "model", False))
    if getattr(a, "strict", False):
        inst.router.strict_discards = True

    auth_token = (getattr(a, "auth_token", None)
                  or os.environ.get("NEURALOSD_API_TOKEN") or None)
    instance_dir = os.path.abspath(a.instance_dir)

    host = getattr(a, "host", "127.0.0.1") or "127.0.0.1"
    print(f"serving instance '{inst.name}' with {len(inst.probes)} probes "
          f"on http://{host}:{a.port}", file=sys.stderr)
    if host not in LOOPBACK:
        # This is not hypothetical: /ask exposes the data behind every probe,
        # and there is no authentication on it yet. Binding wide is a
        # deliberate act, so say so out loud rather than inheriting 0.0.0.0
        # from a default and calling it configured.
        if auth_token:
            print("bound to a non-loopback address WITH token auth — clients "
                  "must send Authorization: Bearer <token>.", file=sys.stderr)
        else:
            print("WARNING: bound to a non-loopback address, and /ask has NO "
                  "authentication. Anyone who can reach this port can read every "
                  "table this instance points at. Use --auth-token (or "
                  "$NEURALOSD_API_TOKEN), put an authenticating proxy in "
                  "front, or bind 127.0.0.1 and tunnel.", file=sys.stderr)
    serve(inst, port=a.port, host=host, auth_token=auth_token,
          instance_dir=instance_dir)


def run_multi(a):
    """Multi-instance serving: one process, many instances, path-routed."""
    from ..multi import MultiInstanceServer, serve_multi

    # same auth resolution as run(): --auth-token wins, then the env var.
    # v2.9.4 resolved nothing here and serve_multi() had no auth parameter,
    # so --auth-token was silently ignored in multi mode.
    auth_token = (getattr(a, "auth_token", None)
                  or os.environ.get("NEURALOSD_API_TOKEN") or None)

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
    host = getattr(a, "host", "127.0.0.1") or "127.0.0.1"
    print(f"  door: http://{host}:{a.port}"
          + ("  [token auth ON]" if auth_token else "  [NO AUTH]"))

    if getattr(a, "strict", False):
        print("  strict refusal policy: ON")

    if host not in LOOPBACK:
        # Same policy as single-instance run(): binding wide is a deliberate
        # act, so say so out loud. With a token the door is locked; without
        # one, every instance's data is readable by anyone who can reach the
        # port — never let that happen silently.
        if auth_token:
            print("bound to a non-loopback address WITH token auth — clients "
                  "must send Authorization: Bearer <token>.", file=sys.stderr)
        else:
            print("WARNING: bound to a non-loopback address, and /ask has NO "
                  "authentication. Anyone who can reach this port can read every "
                  "table every instance here points at. Use --auth-token (or "
                  "$NEURALOSD_API_TOKEN), put an authenticating proxy in "
                  "front, or bind 127.0.0.1 and tunnel.", file=sys.stderr)

    serve_multi(server, host=host, port=a.port, auth_token=auth_token)
