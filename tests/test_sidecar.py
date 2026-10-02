"""Unit tests for the neuralosd sidecar (server, client, and selection logic).

The selection logic is the part that can silently go wrong in two directions:

  * FALSE POSITIVE — the binary decides it can run a probe in-process when it
    actually cannot (or runs the wrong thing), so the user gets a failure or a
    stale answer instead of delegation.
  * FALSE NEGATIVE — the binary refuses or delegates when it could have run
    the probe locally, so a working setup breaks or pays a needless
    subprocess cost.

Every test below is therefore about *which side ran the probe*, not just that
a value came back.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from neuralosd import sidecar as sc
from neuralosd import sidecar_client as scc

SIDE_CMD = [sys.executable, "-m", "neuralosd.sidecar"]


# ── fixtures ───────────────────────────────────────────────────────────────

def _make_instance(tmp_path, body="", name="inst"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "probes.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return str(d)


SIMPLE = """
    from neuralosd import probe

    @probe(description="count rows", triggers=["how many rows", "count"])
    def row_count():
        return {"count": 3}

    @probe(description="sum of sales", triggers=["total sales"],
           args={"region": {"type": "enum", "values": ["north", "south"]}})
    def total_sales(region):
        return {"region": region, "sum": 100}

    @probe(description="slow", triggers=["slow"])
    def slow():
        import time
        time.sleep(30)
        return {"done": True}

    PROBES = [row_count, total_sales, slow]
"""


@pytest.fixture
def simple_instance(tmp_path):
    return _make_instance(tmp_path, SIMPLE)


@pytest.fixture
def numeq_instance(tmp_path):
    """Two probes so we can check routing parity between in-process/sidecar."""
    return _make_instance(tmp_path, """
        from neuralosd import probe

        @probe(description="count rows", triggers=["how many rows", "count"])
        def row_count():
            return {"count": 7}

        @probe(description="list regions", triggers=["list regions", "regions"])
        def regions():
            return {"regions": ["north", "south"]}

        PROBES = [row_count, regions]
    """, name="num")


# ── server: loading ────────────────────────────────────────────────────────

def test_loads_probes_from_PROBES_list(simple_instance):
    assert [p._probe.name for p in sc.load_probe_functions(simple_instance)] == \
        ["row_count", "total_sales", "slow"]


def test_loads_probes_without_explicit_list(tmp_path):
    d = _make_instance(tmp_path, """
        from neuralosd import probe
        @probe(description="x", triggers=["x"])
        def only():
            return {"ok": True}
    """)
    assert [p._probe.name for p in sc.load_probe_functions(d)] == ["only"]


def test_missing_probes_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        sc.load_probe_functions(str(tmp_path / "nope"))


def test_instance_with_no_probes_raises(tmp_path):
    d = _make_instance(tmp_path, "x = 1\n")
    with pytest.raises(ValueError, match="defines no probes"):
        sc.load_probe_functions(d)


def test_probe_meta_is_complete_and_serialisable(simple_instance):
    metas = [sc.probe_meta(p) for p in sc.load_probe_functions(simple_instance)]
    by = {m["name"]: m for m in metas}
    assert by["total_sales"]["args"] == {
        "region": {"type": "enum", "values": ["north", "south"]}}
    assert by["row_count"]["triggers"] == ["how many rows", "count"]
    assert by["row_count"]["tier"] == "in-process"
    assert by["row_count"]["confirm"] is False
    json.dumps(metas)  # must not raise


# ── server: protocol ───────────────────────────────────────────────────────

def test_ping(simple_instance):
    r = sc.Sidecar(simple_instance).handle({"id": 1, "method": "ping"})
    assert r["ok"] is True
    assert r["result"]["probes"] == 3
    assert r["result"]["python"]


def test_menu(simple_instance):
    r = sc.Sidecar(simple_instance).handle({"id": 2, "method": "menu"})
    assert r["ok"] is True
    assert {m["name"] for m in r["result"]} == {"row_count", "total_sales", "slow"}


def test_call_returns_result_and_tool_tag(simple_instance):
    r = sc.Sidecar(simple_instance).handle(
        {"id": 3, "method": "call", "params": {"probe": "row_count", "args": {}}})
    assert r["ok"] is True
    assert r["result"] == {"count": 3, "_tool": "row_count"}


def test_call_passes_arguments(simple_instance):
    r = sc.Sidecar(simple_instance).handle(
        {"id": 4, "method": "call",
         "params": {"probe": "total_sales", "args": {"region": "north"}}})
    assert r["result"] == {"region": "north", "sum": 100, "_tool": "total_sales"}


def test_call_unknown_probe_is_an_error_not_a_crash(simple_instance):
    r = sc.Sidecar(simple_instance).handle(
        {"id": 5, "method": "call", "params": {"probe": "ghost"}})
    assert r["ok"] is False
    assert "unknown probe" in r["error"]


def test_unknown_method_is_an_error(simple_instance):
    r = sc.Sidecar(simple_instance).handle({"id": 6, "method": "nope"})
    assert r["ok"] is False and "unknown method" in r["error"]


def test_probe_exception_is_reported_not_raised(tmp_path):
    d = _make_instance(tmp_path, """
        from neuralosd import probe
        @probe(description="boom", triggers=["boom"])
        def boom():
            raise ValueError("kaboom")
        PROBES = [boom]
    """)
    r = sc.Sidecar(d).handle({"id": 7, "method": "call",
                              "params": {"probe": "boom"}})
    assert r["ok"] is False and "kaboom" in r["error"]


def test_missing_library_error_is_actionable(tmp_path):
    """A probe needing an uninstalled library must name it AND say where."""
    d = _make_instance(tmp_path, """
        from neuralosd import probe
        @probe(description="needs lib", triggers=["lib"])
        def needs_lib():
            import definitely_not_installed_xyz as m
            return {"v": m.__name__}
        PROBES = [needs_lib]
    """)
    r = sc.Sidecar(d).handle({"id": 8, "method": "call",
                              "params": {"probe": "needs_lib"}})
    assert r["ok"] is False
    assert "definitely_not_installed_xyz" in r["error"]
    assert "pip install" in r["error"]


def test_shutdown_marks_stop(simple_instance):
    r = sc.Sidecar(simple_instance).handle({"id": 9, "method": "shutdown"})
    assert r["ok"] is True and r["_stop"] is True


# ── client: discovery ──────────────────────────────────────────────────────

def test_command_from_env(monkeypatch):
    monkeypatch.setenv("NEURALOSD_SIDECAR", "/opt/x/sidecar --flag")
    assert scc.sidecar_command() == ["/opt/x/sidecar", "--flag"]


def test_command_from_path(monkeypatch):
    monkeypatch.delenv("NEURALOSD_SIDECAR", raising=False)
    monkeypatch.setattr(scc.shutil, "which",
                        lambda n: "/usr/bin/neuralosd-sidecar"
                        if n == "neuralosd-sidecar" else None)
    assert scc.sidecar_command() == ["/usr/bin/neuralosd-sidecar"]


def test_command_falls_back_to_python(monkeypatch):
    monkeypatch.delenv("NEURALOSD_SIDECAR", raising=False)
    monkeypatch.setattr(scc.shutil, "which",
                        lambda n: "/usr/bin/python3" if n == "python3" else None)
    assert scc.sidecar_command() == ["/usr/bin/python3", "-m", "neuralosd.sidecar"]


def test_command_none_when_nothing_available(monkeypatch):
    monkeypatch.delenv("NEURALOSD_SIDECAR", raising=False)
    monkeypatch.setattr(scc.shutil, "which", lambda n: None)
    assert scc.sidecar_command() is None


def test_no_command_raises_clear_error(monkeypatch):
    monkeypatch.setattr(scc, "sidecar_command", lambda: None)
    with pytest.raises(scc.SidecarError, match="no sidecar command found"):
        scc.SidecarClient("/tmp").start()


# ── client: real subprocess round-trips ────────────────────────────────────

def test_client_ping_menu_call(simple_instance):
    with scc.SidecarClient(simple_instance, command=SIDE_CMD) as c:
        assert c.ping()["probes"] == 3
        assert {m["name"] for m in c.menu()} == {"row_count", "total_sales", "slow"}
        assert c.call("row_count", {})["count"] == 3
        assert c.call("total_sales", {"region": "south"})["sum"] == 100


def test_client_menu_is_cached(simple_instance):
    with scc.SidecarClient(simple_instance, command=SIDE_CMD) as c:
        assert c.menu() is c.menu()


def test_client_call_unknown_probe_raises(simple_instance):
    with scc.SidecarClient(simple_instance, command=SIDE_CMD) as c:
        with pytest.raises(scc.SidecarError, match="unknown probe"):
            c.call("ghost", {})


def test_client_timeout_on_slow_probe(simple_instance):
    c = scc.SidecarClient(simple_instance, command=SIDE_CMD, timeout=3)
    try:
        c.start()
        with pytest.raises(scc.SidecarError, match="timed out"):
            c.call("slow", {})
    finally:
        c.close()


def test_client_reports_process_death(simple_instance):
    c = scc.SidecarClient(simple_instance, command=SIDE_CMD)
    c.start()
    c.proc.kill()          # simulate the helper being killed mid-session
    c.proc.wait()
    with pytest.raises(scc.SidecarError, match="exited|not accepting"):
        c.call("row_count", {})
    c.close()


def test_client_bad_instance_reports_load_failure(tmp_path):
    d = _make_instance(tmp_path, "raise RuntimeError('bad probes file')\n")
    c = scc.SidecarClient(d, command=SIDE_CMD)
    with pytest.raises(scc.SidecarError, match="failed to load"):
        c.start()
    assert c.proc is None       # must not leak a half-started process


def test_client_nonexistent_command_is_reported(tmp_path):
    c = scc.SidecarClient(_make_instance(tmp_path, SIMPLE),
                          command=["/no/such/binary"])
    with pytest.raises(scc.SidecarError, match="could not launch"):
        c.start()


def test_client_close_is_idempotent(simple_instance):
    c = scc.SidecarClient(simple_instance, command=SIDE_CMD)
    c.start()
    c.close()
    c.close()
    assert c.proc is None


def test_client_close_without_start_is_safe():
    scc.SidecarClient("/tmp", command=SIDE_CMD).close()


# ── selection logic: the FP / FN surface ───────────────────────────────────

def _load(instance_dir, **kw):
    from neuralosd._cmd._common import load_instance
    return load_instance(instance_dir, **kw)


def test_no_sidecar_spawned_when_instance_loads(tmp_path, monkeypatch):
    """FALSE NEGATIVE guard: delegating a loadable instance wastes a process."""
    inst = _make_instance(tmp_path, SIMPLE)
    spawned = []
    real = scc.SidecarClient

    class Spy(real):
        def __init__(self, *a, **k):
            spawned.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(scc, "SidecarClient", Spy)
    loaded = _load(inst)
    assert spawned == []                       # never started a sidecar
    assert loaded.ask("count")["results"][0]["count"] == 3


def test_delegates_when_a_dependency_is_missing(tmp_path, monkeypatch):
    """FALSE POSITIVE guard: if in-process import fails we MUST delegate and
    still answer correctly, rather than reporting the missing module."""
    from neuralosd._cmd import _common
    inst = _make_instance(tmp_path, SIMPLE)

    def boom(probes_py, instance_dir, name):
        raise ModuleNotFoundError("No module named 'pypdf'", name="pypdf")

    monkeypatch.setattr(_common, "_load_probes_in_process", boom)
    monkeypatch.setenv("NEURALOSD_SIDECAR",
                       " ".join(SIDE_CMD))
    loaded = _load(inst)

    env = loaded.ask("count")
    assert env["results"][0]["count"] == 3     # answered via the sidecar
    assert env["probe"] == "row_count"


def test_delegated_instance_preserves_routing(numeq_instance, monkeypatch):
    """The sidecar menu must route the same way the local menu would."""
    from neuralosd._cmd import _common

    local = _load(numeq_instance)
    assert local.ask("how many rows")["probe"] == "row_count"
    assert local.ask("list regions")["probe"] == "regions"

    monkeypatch.setattr(
        _common, "_load_probes_in_process",
        lambda *a, **k: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'x'", name="x")))
    monkeypatch.setenv("NEURALOSD_SIDECAR", " ".join(SIDE_CMD))
    remote = _load(numeq_instance)
    assert remote.ask("how many rows")["probe"] == "row_count"
    assert remote.ask("list regions")["probe"] == "regions"
    assert remote.ask("how many rows")["results"][0]["count"] == 7


def test_missing_dependency_without_sidecar_gives_actionable_exit(
        tmp_path, monkeypatch):
    """No sidecar + missing lib => the clean 'install it' message, not a crash."""
    from neuralosd._cmd import _common
    inst = _make_instance(tmp_path, SIMPLE)

    monkeypatch.setattr(
        _common, "_load_probes_in_process",
        lambda *a, **k: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'pypdf'", name="pypdf")))
    monkeypatch.setenv("NEURALOSD_SIDECAR", "")
    monkeypatch.setattr(scc, "sidecar_command", lambda: None)

    with pytest.raises(SystemExit) as e:
        _load(inst)
    msg = str(e.value)
    assert "pypdf" in msg
    assert "pip install" in msg


def test_broken_sidecar_is_reported_clearly(tmp_path, monkeypatch):
    """A sidecar that exists but fails must not masquerade as success."""
    from neuralosd._cmd import _common
    inst = _make_instance(tmp_path, SIMPLE)
    monkeypatch.setattr(
        _common, "_load_probes_in_process",
        lambda *a, **k: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'pypdf'", name="pypdf")))
    monkeypatch.setenv("NEURALOSD_SIDECAR", "/no/such/sidecar")

    with pytest.raises(SystemExit) as e:
        _load(inst)
    assert "sidecar was found but failed" in str(e.value)


def test_tier_sidecar_probe_is_delegated_even_if_loadable(tmp_path, monkeypatch):
    """An explicitly-delegated probe must not run locally."""
    inst = _make_instance(tmp_path, """
        from neuralosd import probe

        @probe(description="local", triggers=["local"])
        def local_only():
            return {"ran": "in-process"}

        @probe(description="remote", triggers=["remote"], tier="sidecar")
        def go_remote():
            return {"ran": "sidecar"}

        PROBES = [local_only, go_remote]
    """)
    monkeypatch.setenv("NEURALOSD_SIDECAR", " ".join(SIDE_CMD))
    loaded = _load(inst)
    env = loaded.ask("remote")
    assert env["results"][0]["ran"] == "sidecar"     # delegated
    assert env["results"][0]["_tool"] == "go_remote"
    local = loaded.ask("local")
    assert local["results"][0]["ran"] == "in-process"


def test_tier_sidecar_without_sidecar_exits_clearly(tmp_path, monkeypatch):
    inst = _make_instance(tmp_path, """
        from neuralosd import probe
        @probe(description="remote", triggers=["remote"], tier="sidecar")
        def go_remote():
            return {"ran": "sidecar"}
        PROBES = [go_remote]
    """)
    monkeypatch.setattr(scc, "sidecar_command", lambda: None)
    with pytest.raises(SystemExit, match="tier="):
        _load(inst)


# ── end-to-end: the actual promise ─────────────────────────────────────────

def test_sidecar_uses_host_python_not_the_caller(tmp_path):
    """Prove the probe really executes in a *different* interpreter.

    The probe reports os.getpid(); the client process must differ.
    """
    inst = _make_instance(tmp_path, """
        from neuralosd import probe
        @probe(description="pid", triggers=["pid"])
        def who():
            import os
            return {"pid": os.getpid(), "exe": os.path.basename(__import__("sys").executable)}
        PROBES = [who]
    """)
    with scc.SidecarClient(inst, command=SIDE_CMD) as c:
        got = c.call("who", {})
    assert got["pid"] != os.getpid()          # different process
    assert "python" in got["exe"].lower()      # a real interpreter


# ── call-time fallback (lazy imports inside a probe body) ──────────────────

def test_lazy_import_failure_retries_in_sidecar(tmp_path, monkeypatch):
    """FALSE NEGATIVE guard for the lazy-import case.

    A probe usually imports its library INSIDE the function, so the module
    imports fine and the failure only appears at call time. The wrapper must
    catch that and retry in the sidecar instead of surfacing an error.
    """
    from neuralosd import probe as probe_dec
    from neuralosd._cmd import _common

    called = {}

    class FakeClient:
        def __init__(self, instance_dir):
            called["instance_dir"] = instance_dir

        def start(self):
            return self

        def call(self, name, args):
            called["name"] = name
            called["args"] = args
            return {"from": "sidecar", "_tool": name}

    monkeypatch.setattr(scc, "sidecar_command", lambda: ["fake"])
    monkeypatch.setattr(scc, "SidecarClient", FakeClient)

    @probe_dec(description="lazy", triggers=["lazy"])
    def lazy():
        import no_such_module_anywhere  # noqa: F401
        return {"from": "in-process"}

    wrapped = _common._wrap_with_sidecar_fallback([lazy], str(tmp_path))
    out = wrapped[0]()

    assert out == {"from": "sidecar", "_tool": "lazy"}
    assert called["name"] == "lazy"


def test_wrapper_is_transparent_when_the_import_succeeds(tmp_path, monkeypatch):
    """FALSE POSITIVE guard: a healthy probe must run in-process, untouched."""
    from neuralosd import probe as probe_dec
    from neuralosd._cmd import _common

    spawned = []
    monkeypatch.setattr(scc, "sidecar_command",
                        lambda: spawned.append(1) or ["fake"])

    @probe_dec(description="ok", triggers=["ok"])
    def ok():
        return {"from": "in-process"}

    wrapped = _common._wrap_with_sidecar_fallback([ok], str(tmp_path))
    assert wrapped[0]() == {"from": "in-process"}
    assert spawned == []            # the sidecar was never even considered


def test_wrapper_keeps_probe_metadata(tmp_path):
    from neuralosd import probe as probe_dec
    from neuralosd._cmd import _common

    @probe_dec(description="d", triggers=["t"], tier="in-process")
    def thing():
        return {"ok": True}

    wrapped = _common._wrap_with_sidecar_fallback([thing], str(tmp_path))
    assert wrapped[0]._probe is thing._probe
    assert wrapped[0]._probe.name == "thing"
    assert wrapped[0]._probe.triggers == ["t"]
    assert wrapped[0].__name__ == "thing"


def test_wrapper_exits_clearly_without_a_sidecar(tmp_path, monkeypatch):
    from neuralosd import probe as probe_dec
    from neuralosd._cmd import _common

    monkeypatch.setattr(scc, "sidecar_command", lambda: None)

    @probe_dec(description="lazy", triggers=["lazy"])
    def lazy():
        import absent_module_xyz  # noqa: F401
        return {}

    wrapped = _common._wrap_with_sidecar_fallback([lazy], str(tmp_path))
    with pytest.raises(SystemExit) as e:
        wrapped[0]()
    assert "absent_module_xyz" in str(e.value)
    assert "pip install" in str(e.value)


def test_wrapper_does_not_double_wrap_stubs(tmp_path):
    from neuralosd._cmd import _common

    def stub(**kw):
        return {}

    stub._sidecar_backed = True
    out = _common._wrap_with_sidecar_fallback([stub], str(tmp_path))
    assert out[0] is stub


# ── error caching (a transient failure must never be memoised) ─────────────

def _instance(tmp_path, fn):
    from neuralosd import Instance
    return Instance(name="cachetest", probes=[fn], state_dir=str(tmp_path))


def test_errors_are_not_cached(tmp_path):
    """A failure that gets cached poisons the fix: install the library, retry,
    and you would still receive the stale error for the whole TTL."""
    from neuralosd import probe as probe_dec

    state = {"n": 0}

    @probe_dec(description="flaky", triggers=["flaky"])
    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient boom")
        return {"ok": True}

    inst = _instance(tmp_path, flaky)
    env1 = inst.ask("flaky")
    assert "error" in env1["results"][0]        # failed...
    env2 = inst.ask("flaky", use_cache=True)
    assert env2["results"][0].get("ok") is True  # ...and was re-executed
    assert state["n"] == 2
    assert not env2.get("cached")


def test_success_is_still_cached(tmp_path):
    from neuralosd import probe as probe_dec

    state = {"n": 0}

    @probe_dec(description="good", triggers=["good"])
    def good():
        state["n"] += 1
        return {"n": state["n"]}

    inst = _instance(tmp_path, good)
    inst.ask("good")
    env2 = inst.ask("good")
    assert state["n"] == 1          # not re-executed
    assert env2.get("cached") is True


def test_error_envelope_detection():
    from neuralosd.router import _is_error_envelope

    assert _is_error_envelope({"error": "x"})
    assert _is_error_envelope({"mode": "deterministic-error", "results": [1]})
    assert _is_error_envelope({"results": [{"error": "boom"}]})
    assert not _is_error_envelope({"results": [{"count": 1}]})
    assert not _is_error_envelope({"results": [{"error_count": 3}]})


# ── argv splitting (Windows path bug) ──────────────────────────────────────

def test_split_cmd_posix_escapes_are_stripped():
    assert scc._split_cmd("/a/b c/d", posix=True) == ["/a/b", "c/d"]


def test_split_cmd_windows_paths_survive():
    """REGRESSION: posix-mode shlex ate the backslashes in a Windows path, so
    $NEURALOSD_SIDECAR=C:\\tools\\helper.exe launched 'C:toolshelper.exe'."""
    win = r"C:\tools\neuralosd-sidecar.exe"
    assert scc._split_cmd(win, posix=False) == [win]


def test_split_cmd_windows_quoted_path_with_spaces():
    quoted = r'"C:\Program Files\Hyper\neuralosd-sidecar.exe"'
    assert scc._split_cmd(quoted, posix=False) == [
        r"C:\Program Files\Hyper\neuralosd-sidecar.exe"]


def test_split_cmd_windows_path_with_arg():
    cmd = r'"C:\Program Files\H\sidecar.exe" --instance-dir C:\tmp\x'
    assert scc._split_cmd(cmd, posix=False) == [
        r"C:\Program Files\H\sidecar.exe", "--instance-dir", r"C:\tmp\x"]


def test_env_windows_path_not_mangled_on_windows(monkeypatch):
    """On a Windows host the env command must be preserved verbatim."""
    monkeypatch.setattr(scc.os, "name", "nt")
    monkeypatch.setenv("NEURALOSD_SIDECAR", r"C:\tools\sidecar.exe")
    assert scc.sidecar_command() == [r"C:\tools\sidecar.exe"]


def test_disabled_sentinels(monkeypatch):
    for value in ("off", "OFF", "0", "false", "no", "none", "-", "  off  "):
        monkeypatch.setenv("NEURALOSD_SIDECAR", value)
        assert scc.sidecar_command() is None, value
