"""Plugin system: manifests, registry, providers, loading and isolation.

Grows phase by phase with the implementation.
"""
import json
import os
import stat
import sys

import pytest

from neuralosd import probe
from neuralosd.plugins import (PluginError, PluginRecord, Registry,
                               check_compatible, load_manifest,
                               validate_manifest)
from neuralosd.plugins.builtins import builtin_record, builtin_registry


# ── Phase 0: manifests and registry ──────────────────────────────────────

def _manifest(**over):
    data = {
        "schema": "neuralosd.plugin/1", "name": "acme", "version": "1.0.0",
        "plugin_api": "1", "kinds": ["probes"],
        "entrypoint": "acme.plugin:register", "runtime": "inprocess",
    }
    data.update(over)
    return data


def test_manifest_validation_accepts_a_minimal_manifest():
    m = validate_manifest(_manifest(), core_version="3.6.3")
    assert m.name == "acme" and m.plugin_api == "1"


@pytest.mark.parametrize("over,fragment", [
    ({"name": ""}, "name"),
    ({"version": ""}, "version"),
    ({"kinds": ["nonsense"]}, "kind"),
    ({"runtime": "quantum"}, "runtime"),
    ({"entrypoint": None}, "entrypoint"),
    ({"schema": "other/9"}, "schema"),
])
def test_manifest_validation_rejects_bad_input(over, fragment):
    with pytest.raises(PluginError, match=fragment):
        validate_manifest(_manifest(**over))


def test_version_compatibility():
    m = validate_manifest(_manifest(neuralosd=">=3.6,<4"))
    check_compatible(m, "3.6.3")
    with pytest.raises(PluginError, match="requires neuralosd"):
        check_compatible(m, "4.0.0")
    with pytest.raises(PluginError, match="plugin API"):
        validate_manifest(_manifest(plugin_api="2"), core_version="3.6.3")


def test_manifest_loads_from_a_directory(tmp_path):
    d = tmp_path / "plug"
    d.mkdir()
    (d / "neuralosd-plugin.json").write_text(json.dumps(_manifest()),
                                             encoding="utf-8")
    m = load_manifest(d)
    assert m.name == "acme" and m.path == str(d)


def test_registry_rejects_duplicate_probe_and_provider():
    @probe(description="x", triggers=["x"], name="shared")
    def one():
        return {"ok": 1}

    @probe(description="y", triggers=["y"], name="shared")
    def two():
        return {"ok": 2}

    m = validate_manifest(_manifest())
    reg = Registry()
    reg.add(PluginRecord(manifest=m, probes=[one]))
    with pytest.raises(PluginError, match="already provided"):
        reg.add(PluginRecord(manifest=validate_manifest(
            _manifest(name="other")), probes=[two]))


def test_builtin_backends_use_the_same_registry():
    record = builtin_record()
    assert set(record.backends) == {"boxlite", "msb"}
    reg = builtin_registry()
    assert reg.backend("boxlite") is not None
    assert reg.backend_names() == ["boxlite", "msb"]


# ── Phase 1: in-process plugins, providers, CLI ──────────────────────────

PROBE_PLUGIN = '''\
from neuralosd import probe


@probe(description="Acme hello", triggers=["acme hello"])
def hello():
    return {"hello": "world"}


def register(ctx):
    ctx.register_probe(hello)
'''

PROVIDER_PLUGIN = '''\
from neuralosd.plugins.providers import ModelProvider


class FixedProvider(ModelProvider):
    name = "fixed"

    def select(self, question, menu, k=8):
        return {"probe": "incidents_by_region", "args": {"region": "EMEA"}}


def register(ctx):
    ctx.register_provider("fixed", FixedProvider())
'''


def _plugin_dir(tmp_path, name="acme", source=PROBE_PLUGIN, kinds=("probes",)):
    d = tmp_path / name
    d.mkdir()
    (d / "plugin.py").write_text(source, encoding="utf-8")
    (d / "neuralosd-plugin.json").write_text(json.dumps({
        "schema": "neuralosd.plugin/1", "name": name, "version": "1.0.0",
        "plugin_api": "1", "kinds": list(kinds),
        "entrypoint": "plugin:register", "runtime": "inprocess",
    }), encoding="utf-8")
    return d


def _instance_dir(tmp_path, plugins=None, model_provider=None):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text('''\
from neuralosd import probe


@probe(description="Rows", triggers=["rows", "how many rows"])
def rows():
    return {"rows": 3}


@probe(description="Incidents by region", triggers=["incidents by region"],
       args={"region": {"type": "enum", "values": ["EMEA", "APAC"]}})
def incidents_by_region(region):
    return {"region": region, "count": 2}
''', encoding="utf-8")
    cfg = {"plugins": plugins or []}
    if model_provider:
        cfg["model_provider"] = model_provider
    (d / "plugins.json").write_text(json.dumps(cfg), encoding="utf-8")
    return d


def test_probe_plugin_routes_and_audits_provenance(tmp_path):
    from neuralosd._cmd._common import load_instance

    plug = _plugin_dir(tmp_path)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])
    inst = load_instance(str(inst_dir))
    names = {m.name for m in inst.router.metas}
    assert "acme_hello" in names and "rows" in names
    env = inst.ask("acme hello")
    assert env["probe"] == "acme_hello"
    rows = [json.loads(line) for line in
            (inst_dir / "ask_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    plugin_rows = [r for r in rows if r.get("probe") == "acme_hello"]
    assert plugin_rows and plugin_rows[-1]["plugin"] == "acme"
    assert plugin_rows[-1]["plugin_version"] == "1.0.0"


def test_provider_plugin_routes_through_the_gate(tmp_path):
    from neuralosd._cmd._common import load_instance

    plug = _plugin_dir(tmp_path, "fixed-provider", PROVIDER_PLUGIN,
                       kinds=("model_provider",))
    inst_dir = _instance_dir(
        tmp_path, plugins=[{"path": str(plug), "enabled": True}],
        model_provider="fixed")
    inst = load_instance(str(inst_dir))
    assert inst.router.model_fallback is not None
    env = inst.ask("incidents by region")
    assert env["probe"] == "incidents_by_region"
    assert env["results"][0]["region"] == "EMEA"


def test_provider_may_not_invent_a_probe_or_arguments():
    from neuralosd.plugins.providers import ModelProvider, fallback_from_provider

    class BadProbe(ModelProvider):
        def select(self, question, menu, k=8):
            return {"probe": "does_not_exist", "args": {}}

    class BadArgs(ModelProvider):
        def select(self, question, menu, k=8):
            return {"probe": "incidents_by_region", "args": {"region": "MARS"}}

    @probe(description="Incidents", triggers=["incidents"],
           args={"region": {"type": "enum", "values": ["EMEA"]}})
    def incidents(region):
        return {"region": region}

    metas = [incidents._probe]
    assert fallback_from_provider(BadProbe())("incidents", metas) is None
    assert fallback_from_provider(BadArgs())("incidents", metas) is None


def test_provider_spec_parsing():
    from neuralosd.plugins.providers import (OllamaProvider,
                                             OpenAICompatProvider,
                                             DeterministicProvider,
                                             provider_from_spec)

    assert isinstance(provider_from_spec("deterministic"),
                      DeterministicProvider)
    o = provider_from_spec("ollama:qwen3")
    assert isinstance(o, OllamaProvider) and o.model == "qwen3"
    p = provider_from_spec("openai:http://127.0.0.1:8000/v1:llama3.1")
    assert isinstance(p, OpenAICompatProvider)
    assert p.base_url == "http://127.0.0.1:8000/v1"
    assert p.model == "llama3.1"
    with pytest.raises(Exception, match="openai provider needs"):
        provider_from_spec("openai:onlyone")


def test_plugin_cli_new_enable_test_and_doctor(tmp_path, capsys):
    from neuralosd._cmd import plugin as plugin_cli

    out_dir = tmp_path / "made"
    class New:
        plugin_cmd = "new"; name = "made"; out = str(out_dir); kind = "probes"
    assert plugin_cli.run(New()) == 0

    class Test:
        plugin_cmd = "test"; target = str(out_dir)
    assert plugin_cli.run(Test()) == 0
    text = capsys.readouterr().out
    assert "OK made" in text

    inst = _instance_dir(tmp_path)
    class Enable:
        plugin_cmd = "enable"; name = "made"
        instance_dir = str(inst); path = str(out_dir)
    assert plugin_cli.run(Enable()) == 0
    capsys.readouterr()

    class Doctor:
        plugin_cmd = "doctor"; instance_dir = str(inst)
    assert plugin_cli.run(Doctor()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "made" in payload["plugins"]


def test_plugin_lock_pins_and_loader_refuses_a_mismatch(tmp_path):
    from neuralosd._cmd import plugin as plugin_cli
    from neuralosd._cmd._common import load_instance
    from neuralosd.plugins import PluginError

    plug = _plugin_dir(tmp_path)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])

    class Lock:
        plugin_cmd = "lock"; instance_dir = str(inst_dir)
    assert plugin_cli.run(Lock()) == 0
    lock = json.loads((inst_dir / "plugins.lock").read_text(encoding="utf-8"))
    assert "acme" in lock["plugins"]

    # Change the plugin version: the lock must refuse it.
    manifest = json.loads((plug / "neuralosd-plugin.json").read_text(
        encoding="utf-8"))
    manifest["version"] = "2.0.0"
    (plug / "neuralosd-plugin.json").write_text(json.dumps(manifest),
                                                encoding="utf-8")
    with pytest.raises(PluginError, match="locked version"):
        load_instance(str(inst_dir))


def test_plugin_allowlist_refuses_others(tmp_path):
    from neuralosd._cmd._common import load_instance
    from neuralosd.plugins import PluginError

    plug = _plugin_dir(tmp_path)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])
    cfg = json.loads((inst_dir / "plugins.json").read_text(encoding="utf-8"))
    cfg["allow"] = ["someone-else"]
    (inst_dir / "plugins.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(PluginError, match="allow list"):
        load_instance(str(inst_dir))


# ── Phase 2: out-of-process plugins ──────────────────────────────────────

PROCESS_PLUGIN = '''\
import json
import sys

for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    rid = req.get("id")
    if method == "init":
        result = {"probes": [{"name": "hello",
                              "description": "Process hello",
                              "triggers": ["process hello"], "args": {}}],
                  "providers": [{"name": "fixed"}]}
    elif method == "probe":
        result = {"hello": "process"}
    elif method == "select":
        result = {"probe": "hello", "args": {}}
    elif method == "health":
        result = {"ok": True}
    elif method == "shutdown":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}),
              flush=True)
        break
    else:
        result = None
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}),
          flush=True)
'''


def _process_plugin(tmp_path, name="proc-plugin", source=PROCESS_PLUGIN,
                    timeout=None):
    d = _plugin_dir(tmp_path, name, source)
    manifest = json.loads((d / "neuralosd-plugin.json").read_text(
        encoding="utf-8"))
    manifest.update({
        "runtime": "process",
        "command": [sys.executable, str(d / "plugin.py")],
        "entrypoint": None,
        "kinds": ["probes", "model_provider"],
        "permissions": {"timeout_seconds": timeout or 10},
    })
    (d / "neuralosd-plugin.json").write_text(json.dumps(manifest),
                                             encoding="utf-8")
    return d


def test_process_plugin_probes_and_provider(tmp_path):
    from neuralosd._cmd._common import load_instance

    plug = _process_plugin(tmp_path)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])
    inst = load_instance(str(inst_dir))
    assert any(m.name.endswith("_hello") for m in inst.router.metas)
    env = inst.ask("process hello")
    assert env["results"][0]["hello"] == "process"
    assert env["results"][0]["_tool"].endswith("_hello")


def test_process_plugin_timeout_does_not_hang_or_corrupt_core(tmp_path):
    from neuralosd._cmd._common import load_instance
    from neuralosd.plugins import PluginError

    source = PROCESS_PLUGIN.replace(
        'result = {"hello": "process"}',
        'import time; time.sleep(5); result = {"hello": "late"}')
    plug = _process_plugin(tmp_path, source=source, timeout=1)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])
    inst = load_instance(str(inst_dir))
    from neuralosd import NoResults
    try:
        env = inst.ask("process hello")
    except NoResults as exc:
        env = exc.envelope
    assert "timed out" in json.dumps(env), env
    # Core probes still work after a plugin timeout.
    env = inst.ask("how many rows")
    assert env["results"][0]["rows"] == 3


def test_process_plugin_crash_raises_not_guesses(tmp_path):
    from neuralosd._cmd._common import load_instance
    from neuralosd.plugins import PluginError

    source = PROCESS_PLUGIN.replace(
        'result = {"hello": "process"}', 'sys.exit(3)')
    plug = _process_plugin(tmp_path, source=source)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])
    inst = load_instance(str(inst_dir))
    from neuralosd import NoResults
    try:
        env = inst.ask("process hello")
    except NoResults as exc:
        env = exc.envelope
    blob = json.dumps(env)
    assert "PluginError" in blob or "closed stdout" in blob or "exited" in blob


# ── Phase 3: lock, integrity, signatures ─────────────────────────────────

def test_lock_detects_code_change(tmp_path):
    from neuralosd._cmd import plugin as plugin_cli
    from neuralosd._cmd._common import load_instance
    from neuralosd.plugins import PluginError

    plug = _plugin_dir(tmp_path)
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])

    class Lock:
        plugin_cmd = "lock"; instance_dir = str(inst_dir)
    assert plugin_cli.run(Lock()) == 0
    assert load_instance(str(inst_dir))  # locked copy loads
    (plug / "plugin.py").write_text(PROBE_PLUGIN + "\n# changed\n",
                                    encoding="utf-8")
    with pytest.raises(PluginError, match="integrity"):
        load_instance(str(inst_dir))


def test_signed_plugin_verifies_and_tamper_fails(tmp_path):
    import shutil
    import subprocess
    from neuralosd.plugins import PluginError
    from neuralosd.plugins.manifest import signed_payload, tree_digest

    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    plug = _plugin_dir(tmp_path)
    key = plug / "key.pem"
    pub = plug / "plugin.pub"
    sig = plug / "plugin.sig"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA",
                    "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout",
                    "-out", str(pub)], check=True, capture_output=True)
    manifest = load_manifest(plug)
    payload = plug / "payload.txt"
    payload.write_bytes(signed_payload(manifest))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key),
                    "-out", str(sig), str(payload)], check=True,
                   capture_output=True)
    payload.unlink()
    data = json.loads((plug / "neuralosd-plugin.json").read_text(
        encoding="utf-8"))
    data.update({"signature": "plugin.sig", "public_key": "plugin.pub"})
    (plug / "neuralosd-plugin.json").write_text(json.dumps(data),
                                                encoding="utf-8")
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": str(plug),
                                                 "enabled": True}])
    from neuralosd._cmd._common import load_instance
    assert load_instance(str(inst_dir))
    (plug / "plugin.py").write_text(PROBE_PLUGIN + "\n# tampered\n",
                                    encoding="utf-8")
    with pytest.raises(PluginError, match="signature"):
        load_instance(str(inst_dir))


def test_install_verifies_declared_integrity(tmp_path, monkeypatch, capsys):
    from neuralosd._cmd import plugin as plugin_cli
    from neuralosd.plugins import PluginError
    from neuralosd.plugins.manifest import tree_digest

    plug = _plugin_dir(tmp_path)
    digest = tree_digest(plug)
    data = json.loads((plug / "neuralosd-plugin.json").read_text(
        encoding="utf-8"))
    data["integrity"] = digest
    (plug / "neuralosd-plugin.json").write_text(json.dumps(data),
                                                encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    class Install:
        plugin_cmd = "install"; target = str(plug)
    assert plugin_cli.run(Install()) == 0
    capsys.readouterr()

    data["integrity"] = "sha256:" + "0" * 64
    (plug / "neuralosd-plugin.json").write_text(json.dumps(data),
                                                encoding="utf-8")
    assert plugin_cli.run(Install()) == 1
    assert "integrity mismatch" in capsys.readouterr().err


# ── Phase 4: shipped example and docs ────────────────────────────────────

def test_shipped_example_plugin_loads_and_answers(tmp_path):
    from neuralosd._cmd._common import load_instance

    example = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "examples", "plugins", "hello-probe")
    inst_dir = _instance_dir(tmp_path, plugins=[{"path": example,
                                                 "enabled": True}])
    inst = load_instance(str(inst_dir))
    env = inst.ask("hello plugin")
    assert env["probe"] == "hello_hello"
    assert env["results"][0]["hello"].startswith("from the hello plugin")


def test_plugin_docs_exist():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.isfile(os.path.join(root, "docs", "plugins.md"))
    mkdocs = open(os.path.join(root, "mkdocs.yml"), encoding="utf-8").read()
    assert "plugins.md" in mkdocs


SINK_HOOK_PLUGIN = '''\
import json
from neuralosd import probe


@probe(description="Acme hello", triggers=["acme hello"])
def hello():
    return {"hello": "world"}


def register(ctx):
    sink_path = ctx.config["sink"]

    def sink(row):
        with open(sink_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\\n")

    def hook(question, probe_name, args):
        return "policy_blocked" if probe_name.endswith("_hello") else None

    ctx.register_probe(hello)
    ctx.register_audit_sink(sink)
    ctx.register_policy_hook(hook)
'''


def test_audit_sink_receives_rows_and_hook_can_refuse(tmp_path):
    from neuralosd import NoResults
    from neuralosd._cmd._common import load_instance

    sink = tmp_path / "sink.jsonl"
    plug = _plugin_dir(tmp_path, "guarded", SINK_HOOK_PLUGIN,
                       kinds=("probes", "audit_sink", "policy_hook"))
    inst_dir = _instance_dir(tmp_path, plugins=[{
        "path": str(plug), "enabled": True,
        "config": {"sink": str(sink)}}])
    inst = load_instance(str(inst_dir))
    with pytest.raises(NoResults) as exc:
        inst.ask("acme hello")
    assert exc.value.envelope["refusal_reason"] == "policy_blocked"
    env = inst.ask("how many rows")
    assert env["results"][0]["rows"] == 3
    rows = [json.loads(line) for line in sink.read_text(
        encoding="utf-8").splitlines()]
    assert len(rows) >= 2
    assert any(r.get("probe") == "rows" for r in rows)


# ── init --source plugin / deploy --backend plugin ───────────────────────

SOURCE_PLUGIN = '''\
from neuralosd import probe


class Tickets:
    def profile(self, spec):
        return {"kind": "tickets", "rows": int(spec or 0)}

    def probes(self, spec):
        @probe(description="Ticket count", triggers=["ticket count"])
        def ticket_count():
            return {"tickets": int(spec or 0)}
        return [ticket_count]


def register(ctx):
    ctx.register_source("tickets", Tickets())
'''

BACKEND_PLUGIN = '''\
class FakeBackend:
    def deploy(self, instance_dir, *, name, host, port):
        return {"backend": "fake", "name": name, "url": f"http://{host}:{port}",
                "instance_dir": instance_dir}

    @staticmethod
    def available():
        return True


def register(ctx):
    ctx.register_backend("fake", FakeBackend)
'''


def test_init_from_a_plugin_source(tmp_path, capsys):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    from neuralosd._cmd._common import load_instance

    plug = _plugin_dir(tmp_path, "tickets", SOURCE_PLUGIN, kinds=("source",))
    out = tmp_path / "tickets-inst"
    a = SimpleNamespace(source="plugin:tickets:7", out=str(out),
                        name="tickets", plugin_dir=[str(plug)],
                        extra_source=None)
    assert init_cmd.run(a) == 0
    assert (out / "probes.py").is_file()
    assert (out / "plugins.json").is_file()
    inst = load_instance(str(out))
    env = inst.ask("ticket count")
    assert env["probe"] == "ticket_count"
    assert env["results"][0]["tickets"] == 7


def test_init_plugin_source_requires_a_loaded_source(tmp_path):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd

    a = SimpleNamespace(source="plugin:nope:1", out=str(tmp_path / "x"),
                        name="x", plugin_dir=[], extra_source=None)
    with pytest.raises(SystemExit, match="no plugin source"):
        init_cmd.run(a)


def test_deploy_uses_a_plugin_backend(tmp_path, capsys):
    from types import SimpleNamespace
    from neuralosd._cmd import deploy

    plug = _plugin_dir(tmp_path, "fake-backend", BACKEND_PLUGIN,
                       kinds=("backend",))
    inst_dir = _instance_dir(tmp_path)
    a = SimpleNamespace(instance_dir=str(inst_dir), backend="fake",
                        name="box1", port=8899, bind="127.0.0.1",
                        plugin_dir=[str(plug)])
    assert deploy.run(a) == 0
    text = capsys.readouterr().out
    assert "http://127.0.0.1:8899" in text
    assert "deployed 'box1'" in text
