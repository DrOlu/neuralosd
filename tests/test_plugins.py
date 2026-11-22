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
