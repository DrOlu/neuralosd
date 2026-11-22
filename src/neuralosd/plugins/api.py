"""The in-process plugin API (v1).

A plugin receives one context and may add capabilities through it. It cannot
remove or replace core behaviour: the context exposes only registration
methods, never the router, the gate or the audit chain.
"""
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from ..router import plugin_context
from .manifest import PluginError, PluginManifest
from .registry import PluginRecord


class Plugin:
    """Convenience base class; a plain object with `register(ctx)` also works."""

    manifest: Optional[PluginManifest] = None

    def register(self, ctx: "PluginContext") -> None:  # pragma: no cover
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        return {"ok": True}

    def shutdown(self) -> None:
        return None


def _slug(name: str) -> str:
    tail = name.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "_", tail.lower()).strip("_") or "plugin"


class PluginContext:
    def __init__(self, manifest: PluginManifest, instance_dir: str,
                 config: Optional[Dict[str, Any]] = None,
                 read_only: bool = False):
        self.manifest = manifest
        self.instance_dir = instance_dir
        self.config = dict(config or manifest.config or {})
        self.read_only = read_only
        self.logger = logging.getLogger(f"neuralosd.plugin.{manifest.name}")
        self.record = PluginRecord(manifest=manifest)
        self._names: set = set()

    # -- capabilities ------------------------------------------------------
    def register_probe(self, fn: Callable, *, name: Optional[str] = None,
                       golden: Optional[List[Dict]] = None,
                       traps: Optional[List[Dict]] = None) -> Callable:
        meta = getattr(fn, "_probe", None)
        if meta is None:
            raise PluginError(
                f"{self.manifest.name}: register_probe needs a @probe function")
        if name:
            meta.name = name
        else:
            slug = _slug(self.manifest.name)
            if not meta.name.lower().startswith(slug + "_"):
                meta.name = f"{slug}_{meta.name}"
        if meta.name in self._names:
            raise PluginError(f"{self.manifest.name}: duplicate probe "
                              f"{meta.name!r} inside one plugin")
        self._names.add(meta.name)
        record = self.record

        def wrapped(*args, **kwargs):
            provenance = record.digest
            with plugin_context(self.manifest.name, self.manifest.version,
                                provenance):
                return fn(*args, **kwargs)

        wrapped.__name__ = meta.name
        wrapped._probe = meta
        wrapped._plugin = self.manifest.name
        meta.function = wrapped
        record.probes.append(wrapped)
        if golden:
            meta._plugin_golden = list(golden)
        if traps:
            meta._plugin_traps = list(traps)
        return wrapped

    def register_provider(self, name: str, provider: Any) -> None:
        if not hasattr(provider, "select"):
            raise PluginError(f"{self.manifest.name}: a model provider needs "
                              "select(question, menu)")
        self.record.providers[name] = provider

    def register_source(self, name: str, source: Any) -> None:
        if not hasattr(source, "profile") and not callable(source):
            raise PluginError(f"{self.manifest.name}: a source plugin needs "
                              "profile(spec) or a callable")
        self.record.sources[name] = source

    def register_backend(self, name: str, factory: Callable) -> None:
        if not callable(factory):
            raise PluginError(f"{self.manifest.name}: a backend must be a "
                              "factory")
        self.record.backends[name] = factory

    def register_audit_sink(self, sink: Any) -> None:
        if not callable(getattr(sink, "write", None)) and not callable(sink):
            raise PluginError(f"{self.manifest.name}: an audit sink needs "
                              "write(row) or be callable")
        self.record.audit_sinks.append(sink)

    def register_policy_hook(self, hook: Any) -> None:
        if not callable(hook):
            raise PluginError(f"{self.manifest.name}: a policy hook must be "
                              "callable")
        self.record.policy_hooks.append(hook)
