"""`neuralosd serve` — HTTP service for one instance."""
import os
import sys

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def run(a):
    from ..api import serve_api

    raw_dirs = getattr(a, "instance_dir", None)
    dirs = ([raw_dirs] if isinstance(raw_dirs, str)
            else list(raw_dirs or []))
    if not dirs:
        if getattr(a, "instances_dir", None):
            return run_multi(a)
        raise SystemExit("error: --instance-dir is required")
    if getattr(a, "instances_dir", None):
        raise SystemExit("error: use --instance-dir or --instances-dir, "
                         "not both")

    env_key = (os.environ.get("NEURALOSD_SERVE_API_KEY")
               or os.environ.get("NEURALOSD_API_TOKEN"))
    keys = [k for k in (getattr(a, "api_key", None),
                        getattr(a, "auth_token", None), env_key) if k]
    serve_api(
        dirs,
        host=getattr(a, "host", "127.0.0.1") or "127.0.0.1",
        port=getattr(a, "port", 9998),
        api_keys=keys or None,
        api_key_file=getattr(a, "api_key_file", None),
        read_only=bool(getattr(a, "read_only", False)),
        tls_cert=getattr(a, "tls_cert", None),
        tls_key=getattr(a, "tls_key", None),
        timeout=getattr(a, "timeout", 300),
        max_body=getattr(a, "max_body", 1 << 20),
        max_output=getattr(a, "max_output", 8 << 20),
        audit=bool(getattr(a, "audit", True)),
        plan_ttl=getattr(a, "plan_ttl", 86400),
        quiet=bool(getattr(a, "quiet", False)),
        with_model=bool(getattr(a, "model", False)),
        strict=bool(getattr(a, "strict", False)),
        allow_mesh=bool(getattr(a, "allow_mesh", False)),
        model_provider=getattr(a, "model_provider", None),
        plugin_dirs=getattr(a, "plugin_dir", None),
    )


def run_multi(a):
    """Multi-instance serving: one process, many instances, path-routed."""
    from ..multi import MultiInstanceServer, serve_multi

    # same auth resolution as run(): --auth-token wins, then the env var.
    # v2.9.4 resolved nothing here; the first fix wired only --auth-token, so
    # `serve --instances-dir X --api-key K` still started with NO AUTH.
    auth_token = (getattr(a, "auth_token", None)
                  or getattr(a, "api_key", None)
                  or os.environ.get("NEURALOSD_SERVE_API_KEY")
                  or os.environ.get("NEURALOSD_API_TOKEN") or None)
    if getattr(a, "api_key_file", None):
        raise SystemExit(
            "error: --api-key-file is not supported with --instances-dir; "
            "pass --api-key (or --auth-token) instead")

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
