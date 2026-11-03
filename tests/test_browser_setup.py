"""Tests-first: v2.4.1 — `neuralosd browser --setup`.

One command that closes the gap between "neuralosd" and "a working browser
source": check for Node, install the agent-browser CLI via npm WITH CONSENT,
and verify with a live snapshot ping.

Edge cases pinned here:
  - already installed -> no npm run, just the ping
  - consent declined (or EOF on a non-interactive stdin) -> nothing installed
  - node missing -> honest error naming the prerequisite, npm never called
  - npm "succeeds" but the binary is still not on PATH -> honest failure
    (restart-your-shell advice), not a false green
  - installed but the live ping fails (no network etc.) -> reported, not
    swallowed
  - --yes for non-interactive runs (CI, dotfiles)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _fake_runner_ok(args, cap=None, timeout=120):
    if args[:1] == ["open"]:
        return "✓ Example Domain"
    if args[:1] == ["snapshot"]:
        return '- heading "Example Domain" [level=1]\n'
    if args[:1] == ["close"]:
        return "✓ Browser closed"
    return "ok"


def test_setup_when_already_installed_pings_and_skips_npm():
    from neuralosd import websources
    npm_calls = []

    def which_fn(name):
        return f"/usr/local/bin/{name}"          # node AND agent-browser "exist"

    report = websources.setup_browser_tool(
        which_fn=which_fn,
        installer=lambda cmd, timeout=None: npm_calls.append(cmd),
        runner=_fake_runner_ok,
        prompt_fn=lambda: "n")                   # consent irrelevant here
    assert report["installed"] is True
    assert report["already"] is True
    assert npm_calls == [], "an existing install must never re-install"
    assert report["ping_ok"] is True


def test_setup_installs_with_consent_and_verifies():
    from neuralosd import websources
    npm_calls = []

    state = {"installed": False}

    def which_fn(name):
        # stateful: agent-browser "appears on PATH" once npm has run
        if name == "node":
            return "/usr/local/bin/node"
        if name == "npm":
            return "/usr/local/bin/npm"
        return f"/usr/local/bin/{name}" if state["installed"] else None

    def fake_installer(cmd, timeout=None):
        state["installed"] = True
        npm_calls.append(cmd)

    report = websources.setup_browser_tool(
        which_fn=which_fn,
        installer=fake_installer,
        runner=_fake_runner_ok,
        prompt_fn=lambda: "y")
    assert npm_calls, "consented install must invoke the installer"
    cmd = npm_calls[0]
    assert os.path.basename(cmd[0]) == "npm", "install via npm"
    assert cmd[1:3] == ["i", "-g"] and "agent-browser" in cmd
    assert "agent-browser" in npm_calls[0]
    assert report["installed"] is True
    assert report["ping_ok"] is True


def test_setup_yes_flag_skips_the_prompt():
    from neuralosd import websources
    prompts = []
    state = {"installed": False}

    def which_fn(name):
        if name == "node":
            return "/usr/local/bin/node"
        return f"/usr/local/bin/{name}" if state["installed"] else None

    report = websources.setup_browser_tool(
        which_fn=which_fn,
        installer=lambda cmd, timeout=None: state.__setitem__("installed",
                                                              True),
        runner=_fake_runner_ok,
        prompt_fn=lambda: prompts.append("asked") or "y",
        assume_yes=True)
    assert prompts == [], "--yes must not prompt"
    assert report["installed"] is True
    assert report["ping_ok"] is True


def test_setup_declined_installs_nothing():
    from neuralosd import websources
    npm_calls = []
    report = websources.setup_browser_tool(
        which_fn=lambda name: "/usr/local/bin/node" if name == "node"
        else None,
        installer=lambda cmd, timeout=None: npm_calls.append(cmd),
        runner=_fake_runner_ok,
        prompt_fn=lambda: "n")
    assert npm_calls == []
    assert report["installed"] is False
    assert report["declined"] is True


def test_setup_prompt_eof_counts_as_a_decline():
    """A non-interactive stdin (pipelines, CI) must decline safely, never
    crash and never install."""
    from neuralosd import websources

    def eof_prompt():
        raise EOFError

    report = websources.setup_browser_tool(
        which_fn=lambda name: "/usr/local/bin/node" if name == "node"
        else None,
        installer=lambda cmd, timeout=None: None,
        runner=_fake_runner_ok,
        prompt_fn=eof_prompt)
    assert report["declined"] is True
    assert report["installed"] is False


def test_setup_without_node_names_the_prerequisite():
    from neuralosd import websources
    npm_calls = []
    with pytest.raises(SystemExit) as e:
        websources.setup_browser_tool(
            which_fn=lambda name: None,               # no node, no nothing
            installer=lambda cmd, timeout=None: npm_calls.append(cmd),
            runner=_fake_runner_ok,
            prompt_fn=lambda: "y", assume_yes=True)
    assert "node" in str(e.value).lower()
    assert npm_calls == []                            # never even try npm


def test_setup_reports_honourably_when_npm_succeeds_but_binary_is_missing():
    """npm claims success, but the binary is still not on PATH (fresh shell
    needed, npm bin not exported...). A false green here would send the user
    to init, which would then fail confusingly."""
    from neuralosd import websources
    report = websources.setup_browser_tool(
        which_fn=lambda name: "/usr/local/bin/node" if name == "node"
        else None,
        installer=lambda cmd, timeout=None: None,     # npm "worked"
        runner=_fake_runner_ok,
        prompt_fn=lambda: "y", assume_yes=True)
    assert report["installed"] is False
    assert report["ping_ok"] is False
    assert "PATH" in report.get("error", "") or "shell" in report.get(
        "error", "")


def test_setup_reports_a_failed_ping_after_install():
    """Installed but the live ping fails (no network, sandboxed browser) —
    the report must say so, not claim success."""
    from neuralosd import websources

    def which_fn(name):
        return None if name == "agent-browser" else "/usr/local/bin/node"

    state = {"installed": False}

    def which_then_found(name):
        return (f"/usr/local/bin/{name}"
                if name == "node" or state["installed"] else None)

    def fake_installer(cmd, timeout=None):
        state["installed"] = True

    def failing_runner(args, cap=None, timeout=120):
        raise SystemExit("error: browser did not launch")

    report = websources.setup_browser_tool(
        which_fn=which_then_found,
        installer=fake_installer,
        runner=failing_runner,
        prompt_fn=lambda: "y", assume_yes=True)
    assert report["installed"] is True
    assert report["ping_ok"] is False
    assert report.get("error")
