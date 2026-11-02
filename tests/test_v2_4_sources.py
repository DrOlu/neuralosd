"""Tests-first: v2.4.0 — source breadth.

  A/B  agent-browser connector (tier 1 + tier 2): `init --source browser:<url>`
       scaffolds an instance whose bridge drives the installed `agent-browser`
       CLI (accessibility-tree snapshots → rows); a `neuralosd browser` CLI
       passthrough exposes session ops (open/snapshot/click/fill/network);
       capture is injectable so tests run without the tool, and one test
       exercises the REAL CLI when it exists.
  E    authenticated API sources: `init --source api:<url>` with credential
       CAGING — the instance stores only a secret REFERENCE (scrt:/op:/env:),
       resolved at ask time, never written to disk.
  F    multi-source instances with caged joins: `--extra-source` (+ `--on`)
       gives one instance several row sources and a deterministic join.
  G    streaming windows: log instances get a caged `recent_events(minutes)`
       probe — ask over "the last hour".
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance

SNAP = (
    '- heading "Example Domain" [level=1]\n'
    '- paragraph\n'
    '  - StaticText "This domain is for use in examples."\n'
    '- link "Learn more" [ref=e1]\n'
    '- button "Submit" [ref=e2]\n'
)


def _fake_browser_runner(args, cap=None):
    if args[:1] == ["open"]:
        return "✓ Example Domain"
    if args[:1] == ["snapshot"]:
        return SNAP
    raise AssertionError(f"unexpected capture: {args}")


def _init(a_source, a_out, a_name, **extra):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    os.makedirs(a_out, exist_ok=True)
    ns = SimpleNamespace(source=a_source, out=a_out, name=a_name,
                         packages=None, extra_sources=None, join_on=None,
                         since=None, header=None, secret_ref=None,
                         secret_header=None)
    for k, v in extra.items():
        setattr(ns, k, v)
    init_cmd.run(ns)
    return ns


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res


# ── A: agent-browser tier 1 ────────────────────────────────────────────────

def test_browser_source_profiles_the_page(tmp_path, monkeypatch):
    from neuralosd._cmd import init as init_cmd
    from neuralosd import websources
    monkeypatch.setattr(init_cmd, "_capture", _fake_browser_runner)
    # the ask-time bridge re-snapshots through websources._execute:
    monkeypatch.setattr(websources, "_execute", _fake_browser_runner)
    out = str(tmp_path / "inst")
    _init("browser:https://example.com", out, "web")

    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "link" in probes_src                  # roles profiled → caged enums
    inst = load_instance(out)
    env = inst.ask("distinct role", use_cache=False)
    assert env["probe"] == "distinct_role"
    roles = set(_res(env)["distinct"])
    assert {"heading", "link", "button"} <= roles
    env = inst.ask("how many rows", use_cache=False)
    assert _res(env)["count"] == 5               # every snapshot node is a row


def test_browser_rows_parse_refs_roles_and_names():
    from neuralosd.websources import browser_rows
    rows = browser_rows("https://example.com", cap=100,
                        runner=_fake_browser_runner)
    by_ref = {r["ref"]: r for r in rows if r["ref"]}
    assert by_ref["e1"]["role"] == "link"
    assert by_ref["e1"]["name"] == "Learn more"
    assert any(r["role"] == "StaticText" and "examples" in r["name"]
               for r in rows)


def test_browser_source_without_the_tool_names_the_fix(tmp_path, monkeypatch):
    """Same honesty contract as journalctl/wevtutil: a missing tool is named,
    with the install command."""
    import subprocess as sp
    from neuralosd import websources

    def boom(cmd, cap=None, timeout=120):
        raise FileNotFoundError("agent-browser")

    monkeypatch.setattr(websources, "_execute", boom)
    with pytest.raises(SystemExit) as e:
        websources.browser_rows("https://x", runner=None)
    assert "agent-browser" in str(e.value)


@pytest.mark.skipif(pytest.importorskip("shutil").which("agent-browser") is None,
                    reason="agent-browser CLI not installed")
def test_browser_source_against_the_real_cli(tmp_path):
    """Tier 1 against the REAL agent-browser binary (skipped when absent)."""
    from neuralosd.websources import browser_rows
    rows = browser_rows("https://example.com", cap=50)
    assert rows and any(r.get("ref") for r in rows), \
        f"the live page must expose refs; got roles: " \
        f"{sorted({r['role'] for r in rows})}"


# ── B: agent-browser tier 2 — session passthrough ─────────────────────────

def test_browser_cli_passthrough_open_and_snapshot(tmp_path, monkeypatch):
    from neuralosd import websources
    calls = []

    def fake_exec(cmd, cap=None, timeout=120):
        calls.append(cmd)
        return SNAP if "snapshot" in cmd else "✓ ok"

    monkeypatch.setattr(websources, "_execute", fake_exec)
    snap = websources.browser_session_op(
        session="acme", op="snapshot", url="https://example.com")
    assert "Learn more" in snap
    assert any("--session" in c and "acme" in c for c in calls), \
        "session ops must pin the named browser session"

    websources.browser_session_op(session="acme", op="click", target="e1")
    assert any("click" in c and "e1" in " ".join(map(str, c))
               for c in calls)


# ── E: authenticated API sources with credential caging ───────────────────

API_BODY = json.dumps({"items": [
    {"id": 1, "status": "open"}, {"id": 2, "status": "closed"}]})


def test_secret_refs_resolve_without_storing_secrets(tmp_path, monkeypatch):
    from neuralosd import apisources
    monkeypatch.setenv("TEST_TOKEN_24", "sekrit")
    assert apisources.resolve_secret("env:TEST_TOKEN_24") == "sekrit"

    calls = []

    def fake_scrt(cmd, cap=None, timeout=60):
        calls.append(cmd)
        return "vault-value"

    val = apisources.resolve_secret(
        "scrt:/store.scrt:github-pat", runner=fake_scrt)
    assert val == "vault-value"
    assert any("scrt" in c[0] for c in calls)


def test_api_source_profiles_and_refetches_with_caged_credentials(
        tmp_path, monkeypatch):
    from neuralosd import apisources

    monkeypatch.setattr(apisources, "_http_get",
                        lambda url, headers, timeout=60: API_BODY)
    monkeypatch.setenv("TEST_TOKEN_24", "sekrit")
    out = str(tmp_path / "inst")
    _init("api:https://x.test/api", out, "api",
          header="Authorization", secret_ref="env:TEST_TOKEN_24")

    bridge_src = open(os.path.join(out, "bridge.py"), encoding="utf-8").read()
    assert "sekrit" not in bridge_src                    # caged: ref only
    assert "env:TEST_TOKEN_24" in bridge_src

    inst = load_instance(out)
    env = inst.ask("how many rows", use_cache=False)
    assert _res(env)["count"] == 2
    env = inst.ask("distinct status", use_cache=False)
    assert env["probe"] == "distinct_status"


# ── F: multi-source instances with caged joins ────────────────────────────

def test_multi_source_instance_answers_per_source_and_joined(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text("region,units\nNorth,100\nSouth,200\n", encoding="utf-8")
    prices = tmp_path / "prices.csv"
    prices.write_text("region,price\nNorth,10\nSouth,20\n", encoding="utf-8")
    out = str(tmp_path / "inst")
    _init(str(a), out, "ms", extra_sources=[str(prices)],
          join_on="region=region")

    inst = load_instance(out)
    env = inst.ask("how many rows in prices", use_cache=False)
    assert env["probe"] == "count_in"
    assert _res(env)["count"] == 2

    env = inst.ask("joined count", use_cache=False)
    assert env["probe"] == "joined_count"
    assert _res(env)["count"] == 2                   # nested-loop join
    rows = _res(env).get("rows") or []
    assert all(set(r) >= {"region", "units", "price"} for r in rows)


def test_join_is_deterministic_and_value_checking(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text("region,units\nNorth,100\n", encoding="utf-8")
    prices = tmp_path / "prices.csv"
    prices.write_text("region,price\nNorth,10\nEast,9\n", encoding="utf-8")
    out = str(tmp_path / "inst")
    _init(str(a), out, "ms2", extra_sources=[str(prices)],
          join_on="region=region")
    inst = load_instance(out)
    env = inst.ask("joined count", use_cache=False)
    assert _res(env)["count"] == 1                   # East does not join


# ── G: streaming windows ──────────────────────────────────────────────────

def _iso(seconds_ago):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")


def test_log_instances_get_a_caged_recent_events_probe(tmp_path, monkeypatch):
    from neuralosd._cmd import init as init_cmd
    fixture = "\n".join([
        json.dumps({"__REALTIME_TIMESTAMP": "1730512282000000",
                    "_HOSTNAME": "web01", "_COMM": "sshd",
                    "PRIORITY": "6", "MESSAGE": "old event"}),
        json.dumps({"__REALTIME_TIMESTAMP": "1730512344000000",
                    "_HOSTNAME": "web01", "_COMM": "sshd",
                    "PRIORITY": "3", "MESSAGE": "another old event"}),
    ]) + "\n"
    monkeypatch.setattr(init_cmd, "_capture", lambda cmd, cap=None: fixture)
    # the ask-time path re-reads the log through logsources._execute:
    monkeypatch.setattr("neuralosd.logsources._execute",
                        lambda cmd, cap=None, timeout=120: fixture)
    out = str(tmp_path / "inst")
    _init("journal:", out, "jr")

    inst = load_instance(out)
    env = inst.ask("recent events", use_cache=False)
    assert env["probe"] == "recent_events"
    # the fixture timestamps are ancient — nothing is in the last hour:
    assert _res(env)["count"] == 0


def test_recent_events_window_filters_by_ts(tmp_path):
    from neuralosd.logsources import within_window
    assert within_window(_iso(60), minutes=60) is True
    assert within_window(_iso(7200), minutes=60) is False
    assert within_window("", minutes=60) is False    # unparsable = out
