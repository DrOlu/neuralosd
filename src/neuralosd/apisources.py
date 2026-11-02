"""Authenticated API sources with credential CAGING (v2.4.0).

`neuralosd init --source api:<url> --header "Authorization" \\
    --secret-ref "op:vault/item/field"` profiles a JSON API and scaffolds an
instance whose bridge re-fetches at ask time.

The caging contract: the instance files store ONLY the secret REFERENCE
(`scrt:<store>:<key>`, `op:<vault>/<item>/<field>`, `env:<VAR>`) — resolved
through the scrt / 1Password CLIs at ask time, never written to disk. A
secret that leaks into a bridge.py is a bug this module exists to prevent.
"""
import json
import os


def resolve_secret(ref, runner=None):
    """Resolve a secret REFERENCE to its value at ask time.

      env:<VAR>                 — os.environ
      scrt:<store-path>:<key>   — `scrt get` (master password from
                                  $SCRT_PASS, the established store pattern)
      op:<vault>/<item>/<field> — `op read` (1Password CLI)
    """
    if not ref or not isinstance(ref, str):
        raise SystemExit("error: empty secret reference")
    if ref.startswith("env:"):
        var = ref[4:]
        val = os.environ.get(var)
        if val is None:
            raise SystemExit(f"error: secret ref {ref!r} — "
                             f"environment variable {var!r} is not set")
        return val
    if ref.startswith("scrt:"):
        _, store, key = ref.split(":", 2)
        password = os.environ.get("SCRT_PASS", "")
        return _run(["scrt", "get", key, "--storage", "local",
                     "--local-path", store, "-p", password],
                    f"scrt ref {ref!r}", runner)
    if ref.startswith("op:"):
        uri = "op://" + ref[3:]
        return _run(["op", "read", uri], f"1Password ref {ref!r}", runner)
    raise SystemExit(f"error: unknown secret reference scheme: {ref!r} "
                     f"(supported: env:, scrt:, op:)")


def _run(cmd, label, runner):
    if runner is not None:
        return (runner(cmd) or "").strip()
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except FileNotFoundError:
        raise SystemExit(f"error: {label} needs "
                         f"{cmd[0]!r}, which is not installed")
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SystemExit(f"error: {label} failed: {err[:200]}")
    return decode_out(r.stdout).strip()


def decode_out(data):
    if isinstance(data, str):
        return data
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8", errors="replace")


def _http_get(url, headers, timeout=60):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def api_get(url, headers=None, cap=None, fetcher=None, timeout=60):
    """GET the URL (injectable `fetcher(url, headers) -> text` for tests)."""
    if fetcher is not None:
        return fetcher(url, headers)
    return _http_get(url, headers, timeout=timeout)


def api_rows(url, header_name=None, secret_ref=None, cap=500, fetcher=None,
             timeout=60):
    """Fetch and normalize the API response to ROWS.

    A JSON list is the rows. A dict with exactly one list value is that
    list. Anything else is a single row. Non-JSON is an honest error.
    """
    headers = {}
    if header_name and secret_ref:
        headers[header_name] = resolve_secret(secret_ref)
    body = api_get(url, headers, fetcher=fetcher, timeout=timeout)
    try:
        data = json.loads(body)
    except ValueError:
        raise SystemExit(f"error: {url} did not return JSON — API sources "
                         f"need a JSON endpoint")
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            rows = [r for r in lists[0] if isinstance(r, dict)]
        else:
            rows = [data]
    else:
        rows = []
    return rows[:cap or 500]
