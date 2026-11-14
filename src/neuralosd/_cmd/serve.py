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
        # Fail closed. /ask exposes every probe's data; binding wide without
        # a credential is not a default we will let an operator inherit.
        from ..agentic import agent_tokens
        if not auth_token and not agent_tokens(instance_dir):
            raise SystemExit(
                f"error: refusing to bind {host} without a credential — pass "
                f"--auth-token (or $NEURALOSD_API_TOKEN), configure per-agent "
                f"tokens in acl.json, or bind 127.0.0.1")
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

    if host not in LOOPBACK and not auth_token:
        raise SystemExit(
            f"error: refusing to bind {host} without a credential — pass "
            f"--auth-token (or $NEURALOSD_API_TOKEN) or bind 127.0.0.1")

    serve_multi(server, host=host, port=a.port, auth_token=auth_token)
