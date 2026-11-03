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


# ── setup: consent-gated install + live verification (v2.4.1) ─────────────

def setup_browser_tool(which_fn=None, installer=None, runner=None,
                       prompt_fn=None, assume_yes=False):
    """`neuralosd browser --setup` — close the gap between "neuralosd" and
    "a working browser source":

      1. already installed? -> never re-install, just ping
      2. Node/npm on PATH?  -> else an error that names the prerequisite
      3. consent            -> a prompt (input()), skipped by --yes;
                               EOF on a non-interactive stdin = decline
      4. npm i -g agent-browser
      5. verify the binary REALLY landed on PATH (npm "success" with no
         binary is a classic false green — report it, advise the shell)
      6. live snapshot ping (open example.com + snapshot + close)

    Everything injectable (which_fn / installer / runner / prompt_fn) so the
    whole flow is unit-testable without Node, npm, or a browser.
    """
    import shutil
    which_fn = which_fn or shutil.which
    report = {"installed": False, "already": False, "declined": False,
              "ping_ok": False, "node": None, "error": None}

    ab = which_fn("agent-browser")
    node = which_fn("node")
    report["node"] = node

    if not ab:
        if not node:
            raise SystemExit(
                "error: neuralosd browser --setup needs Node.js (v18+) and "
                "npm on PATH first — the agent-browser CLI is an npm package."
                "\n    install Node from https://nodejs.org and re-run")
        if not assume_yes:
            try:
                ask = prompt_fn or (lambda: input(
                    "Install the agent-browser CLI globally via npm "
                    "(npm i -g agent-browser)? [y/N] "))
                answer = ask()
            except EOFError:
                answer = ""          # non-interactive stdin declines safely
            if answer.strip().lower() not in ("y", "yes"):
                report["declined"] = True
                return report
        npm = which_fn("npm")
        if installer is None and not npm:
            # the real installer needs npm; an injected one does not (tests,
            # exotic environments handle their own tooling)
            raise SystemExit("error: npm not found on PATH (Node is "
                             "installed but npm is missing?)")
        installer = installer or _npm_install
        try:
            # an injected installer gets the plain package spec (its tooling
            # resolves npm itself); the real one gets the resolved path
            installer([(npm or "npm"), "i", "-g", "agent-browser"],
                      timeout=600)
        except Exception as e:                             # noqa: BLE001
            report["error"] = f"npm install failed: {str(e)[:300]}"
            return report
        ab = which_fn("agent-browser")
        if not ab:
            report["error"] = (
                "npm reported success but agent-browser is still not on "
                "PATH — restart your shell, or check npm's global bin dir "
                "(npm prefix -g) and add it to PATH")
            return report
        report["installed"] = True
    else:
        report["already"] = True
        report["installed"] = True

    # the live proof: a real open + snapshot, then clean up
    _exec = runner or _execute
    try:
        _exec(["open", "https://example.com"], None)
        text = _exec(["snapshot"], None)
        report["ping_ok"] = bool(text and text.strip())
        try:
            _exec(["close"], None)
        except Exception:                                  # noqa: BLE001
            pass                                          # cleanup is best-effort
    except (Exception, SystemExit) as e:
        report["ping_ok"] = False
        report["error"] = (f"installed, but the live snapshot ping failed: "
                           f"{str(e)[:200]} (offline? sandboxed browser?)")
    return report


def _npm_install(cmd, timeout=600):
    import subprocess
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err[:300] or "npm exited non-zero")
