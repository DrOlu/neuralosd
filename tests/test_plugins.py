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
