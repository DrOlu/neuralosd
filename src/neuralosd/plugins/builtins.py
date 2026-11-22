"""Built-ins registered through the same plugin contract as third parties.

Dogfooding is the point: if boxlite/msb and the Ollama/OpenAI-compatible
providers use the public registry, a third-party plugin has the same power —
and the same limits — as the shipped ones.
"""
from .manifest import PluginManifest
from .registry import PluginRecord, Registry


def _backend_factories():
    from ..backends.boxlite_backend import BoxLiteBackend
    from ..backends.msb_backend import MSBBackend
    return {"boxlite": BoxLiteBackend, "msb": MSBBackend}


def builtin_record() -> PluginRecord:
    factories = _backend_factories()
    manifest = PluginManifest(
        name="neuralosd.builtins", version="1", kinds=["backend"],
        entrypoint="neuralosd.plugins.builtins:builtin_record",
        description="Sandbox backends shipped with neuralOSd.")
    record = PluginRecord(manifest=manifest, backends=dict(factories))
    return record


def builtin_registry() -> Registry:
    reg = Registry()
    reg.add(builtin_record())
    return reg


def get_backend(name: str, **kwargs):
    factory = builtin_record().backends.get(name)
    if factory is None:
        raise ValueError(f"unknown backend: {name}")
    return factory(**kwargs)
