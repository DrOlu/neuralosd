"""agent-browser connector — the browser as a data source (v2.4.0).

Tier 1: `neuralosd init --source browser:<url>` profiles the page through
the installed `agent-browser` CLI (https://github.com/vercel/agent-browser):
the accessibility-tree snapshot becomes ROWS ({role, name, ref, depth}), the
roles become caged enums, and the generated bridge re-snapshots at ask time.

Tier 2: `browser_session_op` drives a NAMED persistent session (open /
snapshot / click / fill / network) — the same CLI, pinned to one browser
session, so login state survives across probes.

Same honesty contract as the log connectors: the tool must exist where the
probes run; a missing `agent-browser` binary is an error that names the fix.
Capture is injectable (`runner(args, cap) -> stdout`) so everything is
unit-testable without a browser.
"""
import re

_SNAP_RE = re.compile(
    r"^(?P<indent>\s*)-\s+(?P<role>\w+)"
    r"(?:\s+\"(?P<name>[^\"]*)\")?"
    r"(?:\s+\[(?P<attrs>[^\]]*)\])?\s*$")

_REF_RE = re.compile(r"ref=(\S+)")


def browser_rows(url, cap=500, runner=None):
    """Open the URL and return snapshot nodes as rows.

    Every accessibility node is a row: {role, name, ref, depth}. refs are
    the agent-browser action handles — a caged enum of them IS the menu of
    things this instance can act on.
    """
    _exec = runner if runner is not None else _execute
    try:
        _exec(["open", url], cap)
        text = _exec(["snapshot"], cap)
    except FileNotFoundError:
        raise SystemExit(
            "error: the agent-browser CLI is not available on this host.\n"
            "    install it with:  npm i -g agent-browser\n"
            "    (https://github.com/vercel/agent-browser)")
    rows = []
    for line in (text or "").splitlines():
        m = _SNAP_RE.match(line)
        if not m:
            continue
        depth = len(m.group("indent")) // 2
        attrs = m.group("attrs") or ""
        ref_m = _REF_RE.search(attrs)
        rows.append({
            "role": m.group("role"),
            "name": m.group("name") or "",
            "ref": ref_m.group(1) if ref_m else "",
            "depth": depth,
        })
        if len(rows) >= cap:
            break
    return rows


def browser_session_op(session, op, url=None, target=None, text=None,
                       runner=None):
    """Tier 2: one operation against a NAMED browser session.

    op: open | snapshot | click | fill | type | network | text
    The `--session <name>` flag pins every command to the same browser, so
    login state and SPA navigation survive across probes.
    """
    base = ["--session", session] if session else []
    if op == "open":
        if not url:
            raise SystemExit("error: browser open needs --url")
        return _execute(base + ["open", url])
    if op == "snapshot":
        if url:
            _execute(base + ["open", url])
        return _execute(base + ["snapshot"])
    if op in ("click", "fill", "type"):
        if not target:
            raise SystemExit(f"error: browser {op} needs --target <ref|sel>")
        cmd = base + [op, ("@" + target if re.fullmatch(r"e\d+", target)
                           else target)]
        if op in ("fill", "type"):
            if text is None:
                raise SystemExit(f"error: browser {op} needs --text")
            cmd.append(text)
        return _execute(cmd)
    if op == "network":
        return _execute(base + ["network"])
    if op == "text":
        return _execute(base + ["get", "text", target] if target
                        else base + ["read"])
    raise SystemExit(f"error: unknown browser op {op!r} "
                     f"(open|snapshot|click|fill|type|network|text)")


def _execute(args, cap=None, timeout=120):
    import subprocess
    cmd = ["agent-browser"] + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit(
            "error: the agent-browser CLI is not available on this host.\n"
            "    install it with:  npm i -g agent-browser\n"
            "    (https://github.com/vercel/agent-browser)")
    out = r.stdout or b""
    if out[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = out.decode("utf-16")
    elif b"\x00" in out[:4096]:
        try:
            text = out.decode("utf-16-le")
        except UnicodeDecodeError:
            text = out.decode("utf-8", errors="replace")
    else:
        text = out.decode("utf-8", errors="replace")
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SystemExit(f"error: {' '.join(cmd)} failed: {err[:200]}")
    return text
