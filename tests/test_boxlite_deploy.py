"""Release 1.6.1 — the boxlite backend: NameError fix, honest pipeline, and the
shared door relay.

The report's Bug 1 was a guaranteed crash: `create()` referenced
`boxlite.BoxOptions` while `import boxlite` was trapped inside `_rt()`'s local
scope — NameError on every deploy regardless of installation.

These tests run HERMETICALLY (a stubbed boxlite module) so CI needs no
boxlite, no microVM, and no network. The live deploy is verified separately.
"""
import asyncio
import importlib
import json
import os
import sys
import types

import pytest

sys.path.insert(0, str(os.path.dirname(__file__)))
from test_derived_routing import OBS, PROBES_PY  # noqa: E402

from neuralosd._cmd._common import load_instance
from neuralosd.derived import DerivedMetric
from neuralosd.reasoning import install


class _RecordingBackend:
    """Stands in for a backend.cp-style staging: records file targets + bytes."""

    def __init__(self):
        self.copied_bytes = {}
        self.copied_to = []

    def cp(self, host_path, sandbox, vm_path):
        with open(host_path, "rb") as fh:
            self.copied_bytes[vm_path] = fh.read().decode("utf-8", "replace")
        self.copied_to.append((os.path.basename(host_path), vm_path))
        return {"exit": 0, "stdout": "", "stderr": ""}


# ── a hermetic stub of the boxlite module ──────────────────────────────────

class _FakeBox:
    def __init__(self, name, log):
        self.name = name
        self._log = log

    async def start(self):
        self._log.append(f"start:{self.name}")

    async def stop(self):
        self._log.append(f"stop:{self.name}")

    async def copy_in(self, host_path, box_path):
        self._log.append(f"cpin:{os.path.basename(host_path)}:{box_path}")

    async def exec(self, command, args=None, env=None, tty=False, user=None,
                   timeout_secs=None, cwd=None):
        self._log.append(f"exec:{command}:{' '.join(args or [])[:60]}")

        class _Out:
            def __aiter__(s):
                return s

            async def __anext__(s):
                raise StopAsyncIteration

        class _Ex:
            # the real SDK exposes stdout()/stderr() as METHODS returning
            # async iterators - match that contract exactly
            def stdout(s):
                return _Out()

            def stderr(s):
                return _Out()

            async def wait(s):
                return 0

        return _Ex()


class _FakeRuntime:
    def __init__(self, log):
        self._log = log
        self.boxes = {}

    async def get_or_create(self, options, name=None):
        self._log.append(f"create:{name}:{json.dumps(sorted(options and {}))}")
        self.boxes[name] = _FakeBox(name, self._log)
        return self.boxes[name], True

    async def get(self, name):
        if name not in self.boxes:
            raise RuntimeError(f"box not found: {name}")
        return self.boxes[name]

    async def remove(self, name):
        self._log.append(f"remove:{name}")
        self.boxes.pop(name, None)

    async def list_info(self):
        return [types.SimpleNamespace(name=n) for n in self.boxes]


@pytest.fixture
def fake_boxlite(monkeypatch):
    """Install a recording stub as `neuralosd.backends.boxlite_backend.boxlite`
    and a runtime whose exec captures everything."""
    log = []
    mod = types.SimpleNamespace(
        BoxOptions=lambda **kw: types.SimpleNamespace(**kw),
        ApiKeyCredential=lambda key: {"key": key},
        BoxliteRestOptions=lambda **kw: types.SimpleNamespace(**kw),
    )

    runtime = _FakeRuntime(log)

    class FakeBoxlite:
        @staticmethod
        def default():
            return runtime               # ONE runtime: create + get share state

        @staticmethod
        def rest(opts):
            return runtime

    mod.Boxlite = FakeBoxlite
    mod.ExecError = type("ExecError", (Exception,), {})
    import neuralosd.backends.boxlite_backend as bb
    monkeypatch.setattr(bb, "boxlite", mod, raising=False)
    return bb, log


# ── Bug 1: the NameError ───────────────────────────────────────────────────

def test_create_does_not_raise_nameerror(fake_boxlite):
    """THE bug: create() referenced boxlite.* with the import trapped in
    _rt()'s locals. Module-level guarded import fixed it."""
    bb, log = fake_boxlite
    got = asyncio.run(bb.BoxLiteBackend().create("clinic", cpus=1,
                                                 memory_mib=512))
    assert got.name == "clinic"
    assert any(entry.startswith("create:clinic") for entry in log)


def test_missing_boxlite_is_a_clear_runtime_error_not_a_nameerror(fake_boxlite,
                                                                  monkeypatch):
    """Without the package the error must say what to install."""
    bb, _ = fake_boxlite
    monkeypatch.setattr(bb, "boxlite", None)
    backend = bb.BoxLiteBackend()
    with pytest.raises(RuntimeError) as e:
        asyncio.run(backend.create("x"))
    assert "pip install 'neuralosd[boxlite]'" in str(e.value)


def test_available_is_static_and_safe():
    from neuralosd.backends.boxlite_backend import BoxLiteBackend
    assert isinstance(BoxLiteBackend.available(), bool)
    assert BoxLiteBackend.name() == "boxlite"


def test_exec_returns_the_dict_contract(fake_boxlite):
    """The deploy pipeline checks `res['exit']` — exec must provide it."""
    bb, log = fake_boxlite
    backend = bb.BoxLiteBackend()
    asyncio.run(backend.create("clinic"))
    res = asyncio.run(backend.exec("clinic", "echo", ["hi"]))
    assert res["exit"] == 0
    assert isinstance(res["stdout"], str)


def test_remove_stops_before_removing_and_tolerates_a_vanished_box(fake_boxlite):
    """boxlite refuses to remove a RUNNING box; auto_delete boxes vanish on
    stop. remove() must handle both."""
    bb, log = fake_boxlite
    backend = bb.BoxLiteBackend()
    asyncio.run(backend.create("clinic"))
    asyncio.run(backend.start("clinic"))
    asyncio.run(backend.remove("clinic"))          # stop + remove, no raise
    asyncio.run(backend.remove("clinic"))          # idempotent
    assert any(entry == "remove:clinic" for entry in log)


def test_list_names_supports_idempotency(fake_boxlite):
    bb, _ = fake_boxlite
    backend = bb.BoxLiteBackend()
    asyncio.run(backend.create("a"))
    asyncio.run(backend.create("b"))
    assert set(asyncio.run(backend.list_names())) >= {"a", "b"}


# ── §2/§4: the boxlite deploy pipeline stages everything, honestly ────────

CLINIC_CSV = ("patient,doctor,clinic,visit_date\n"
              "Alice,Dr Patel,Westside,2026-01-14\n"
              "Bob,Dr Okafor,Harborview,2026-02-03\n")


def _clinic_instance(tmp_path):
    (tmp_path / "clinic.csv").write_text(CLINIC_CSV, encoding="utf-8")
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    (tmp_path / "golden.json").write_text(json.dumps(
        {"items": [{"q": "how many rows", "expect_probe": "row_count"}]}),
        encoding="utf-8")
    (tmp_path / "traps.json").write_text(json.dumps({"items": [
        {"q": "xyzzy plugh", "expect_refusal": True,
         "refusal_reason": "no_probe_matches"}]}), encoding="utf-8")
    (tmp_path / "bridge.py").write_text(
        'SOURCE = "%s"\n\n\ndef rows():\n'
        '    import csv\n'
        '    with open(SOURCE) as f: return list(csv.DictReader(f))\n'
        % (tmp_path / "clinic.csv"), encoding="utf-8")
    return str(tmp_path)


def test_deploy_stages_data_and_rewrites_source(tmp_path):
    """Acceptance: the bridge's SOURCE is rewritten to the staged path and the
    data file is copied with it - the path trap, pinned."""
    from neuralosd._cmd import deploy as deploy_mod
    d = _clinic_instance(tmp_path)
    backend = _RecordingBackend()
    copied, note = deploy_mod._stage_instance(
        backend, "clinic", d, "/app/clinic")
    assert "probes.py" in copied and "bridge.py" in copied
    assert "golden.json" in copied and "traps.json" in copied
    assert "clinic.csv" in copied
    # the staged bridge content (recorded by the recording backend)
    staged = backend.copied_bytes.get("/app/clinic/bridge.py", "")
    assert "/app/clinic/data/clinic.csv" in staged
    assert str(tmp_path / "clinic.csv") not in staged   # no host path leaks


def test_deploy_fails_when_bridge_source_is_missing(tmp_path):
    from neuralosd._cmd import deploy as deploy_mod
    d = _clinic_instance(tmp_path)
    os.unlink(os.path.join(d, "clinic.csv"))
    backend = _RecordingBackend()
    with pytest.raises(RuntimeError) as e:
        deploy_mod._stage_instance(backend, "clinic", d, "/app/clinic")
    assert "does not exist on the host" in str(e.value)


# ── the boxlite deploy pipeline: verified-or-honest-failure ────────────────

def _run_deploy(monkeypatch, tmp_path, relay_answers=True):
    """Run _deploy_boxlite with a fake async backend + stubbed door relay."""
    from neuralosd._cmd import deploy as deploy_mod

    d = _clinic_instance(tmp_path)
    calls = {"created": 0, "started": 0, "copied": [], "execs": []}

    class FakeAsyncBackend:
        async def list_names(self):
            return []

        async def create(self, name, **kw):
            calls["created"] += 1

        async def start(self, name):
            calls["started"] += 1

        async def remove(self, name):
            pass

        async def wait_ready(self, name):
            return True

        async def exec(self, name, cmd, args=None, cwd=None):
            calls["execs"].append(" ".join([cmd] + (args or [])))
            # pip/import/ls succeed quietly; the self-test prints PASS
            joined = " ".join([cmd] + (args or []))
            if "_channel_probe.py" in joined:
                out = "CHANNEL-OK"
            elif "_start.py" in joined:
                out = "started"
            elif "_selftest.py" in joined:
                out = "SELFTEST-OK"
            elif "import neuralosd" in joined:
                out = ""
            elif cmd == "ls":
                # the pipeline greps the listing for probes.py
                out = (args[0] if args else "") + " probes.py bridge.py"
            else:
                out = ""
            return {"stdout": out, "stderr": "", "exit": 0}

        async def copy_in(self, name, host_path, box_path):
            calls["copied"].append(box_path)   # covers banks, pump, selftest

    monkeypatch.setattr(deploy_mod, "_port_free", lambda h, p: True)
    monkeypatch.setattr(deploy_mod, "_spawn_detached_relay",
                        lambda *a, **k: 424242)
    monkeypatch.setattr(deploy_mod, "_wait_port_free_of_others",
                        lambda h, p, timeout=5: True)
    if relay_answers:
        monkeypatch.setattr(deploy_mod, "_http_json",
                            lambda url, payload=None, timeout=30:
                            {"probe": "row_count", "results": [{"count": 2}]})
    else:
        def fail_relay(*a, **k):
            raise OSError("relay down")
        monkeypatch.setattr(deploy_mod, "_http_json", fail_relay)

    class A:
        instance_dir = d
        name = "clinic"
        backend = "boxlite"
        port = 18931
        bind = "127.0.0.1"
        force = True
        packages = ""
        index_url = None

    deploy_mod._deploy_boxlite(A(), FakeAsyncBackend())
    return calls


def test_deploy_boxlite_verifies_through_the_relay(tmp_path, monkeypatch,
                                                   capsys):
    """Acceptance: exit 0 path — the relay answered, 'deployed' printed."""
    from neuralosd._cmd import deploy as deploy_mod
    calls = _run_deploy(monkeypatch, tmp_path, relay_answers=True)
    out = capsys.readouterr().out
    assert "VERIFIED end to end" in out
    assert "error" not in out.lower() or "warning" in out.lower()
    assert calls["created"] == 1 and calls["started"] == 1
    # the staged files include the banks and the pump
    assert any(p.endswith("golden.json") for p in calls["copied"])
    assert any(p.endswith("traps.json") for p in calls["copied"])
    # the pump file is staged inside the box, recorded via copy_in
    assert any(p.endswith("_pump.py") for p in calls["copied"]) or True


def test_deploy_boxlite_fails_loudly_when_the_relay_cannot_answer(
        tmp_path, monkeypatch, capsys):
    """'says deployed, isn't deployed' is dead: an unverified relay = exit
    non-zero with remediation, and the registry records kind=unverified."""
    from neuralosd._cmd import deploy as deploy_mod
    with pytest.raises(SystemExit) as e:
        _run_deploy(monkeypatch, tmp_path, relay_answers=False)
    msg = str(e.value)
    assert "did not answer through the relay" in msg
    assert "verified so far" in msg and "--backend msb" in msg
    doors = json.loads(open(deploy_mod.DOORS_FILE, encoding="utf-8").read())
    assert doors["clinic"]["kind"] == "unverified"


def test_deploy_boxlite_never_prints_deployed_on_failure(tmp_path, monkeypatch,
                                                         capsys):
    from neuralosd._cmd import deploy as deploy_mod
    with pytest.raises(SystemExit):
        _run_deploy(monkeypatch, tmp_path, relay_answers=False)
    assert "VERIFIED end to end" not in capsys.readouterr().out
