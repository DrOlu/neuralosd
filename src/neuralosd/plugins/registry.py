"""The plugin registry — one place where capabilities, providers and adapters
are collected, namespaced and attributed.

The core still owns routing, the refusal gate, the signed envelope and the
audit chain. The registry only lets a plugin ADD capabilities; it can never
remove or override a core one.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .manifest import PluginError, PluginManifest


@dataclass
class PluginRecord:
    manifest: PluginManifest
    probes: List[Callable] = field(default_factory=list)
    providers: Dict[str, Any] = field(default_factory=dict)
    sources: Dict[str, Any] = field(default_factory=dict)
    backends: Dict[str, Callable] = field(default_factory=dict)
    audit_sinks: List[Any] = field(default_factory=list)
    policy_hooks: List[Any] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def digest(self) -> str:
        """A stable digest of the plugin's declared identity + code location."""
        blob = json.dumps({
            "manifest": self.manifest.to_dict(),
            "path": self.manifest.path}, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


class Registry:
    def __init__(self):
        self.records: Dict[str, PluginRecord] = {}
        self._probe_owner: Dict[str, str] = {}
        self._providers: Dict[str, tuple] = {}
        self._sources: Dict[str, tuple] = {}
        self._backends: Dict[str, Callable] = {}
        self._sinks: List[tuple] = []
        self._hooks: List[tuple] = []

    # -- registration -----------------------------------------------------
    def add(self, record: PluginRecord) -> PluginRecord:
        if record.name in self.records:
            raise PluginError(f"plugin already registered: {record.name}")
        self.records[record.name] = record
        for fn in record.probes:
            self.register_probe(record, fn)
        for name, provider in record.providers.items():
            self.register_provider(record, name, provider)
        for name, source in record.sources.items():
            self.register_source(record, name, source)
        for name, backend in record.backends.items():
            self.register_backend(record, name, backend)
        for sink in record.audit_sinks:
            self._sinks.append((record.name, sink))
        for hook in record.policy_hooks:
            self._hooks.append((record.name, hook))
        return record

    def register_probe(self, record: PluginRecord, fn: Callable) -> None:
        meta = getattr(fn, "_probe", None)
        if meta is None:
            raise PluginError(f"{record.name}: registered object is not a probe")
        owner = self._probe_owner.get(meta.name)
        if owner and owner != record.name:
            raise PluginError(
                f"probe {meta.name!r} is already provided by {owner!r}")
        self._probe_owner[meta.name] = record.name
        if fn not in record.probes:
            record.probes.append(fn)

    def register_provider(self, record, name, provider):
        if name in self._providers:
            raise PluginError(f"model provider already registered: {name}")
        self._providers[name] = (record.name, provider)
        record.providers[name] = provider

    def register_source(self, record, name, source):
        if name in self._sources:
            raise PluginError(f"source already registered: {name}")
        self._sources[name] = (record.name, source)
        record.sources[name] = source

    def register_backend(self, record, name, backend):
        if name in self._backends:
            raise PluginError(f"backend already registered: {name}")
        self._backends[name] = backend
        record.backends[name] = backend

    # -- lookup -----------------------------------------------------------
    def probes(self) -> List[Callable]:
        return [fn for record in self.records.values()
                for fn in record.probes]

    def providers(self) -> Dict[str, Any]:
        return {name: provider for name, (_owner, provider)
                in self._providers.items()}

    def provider(self, name: str):
        item = self._providers.get(name)
        return item[1] if item else None

    def source(self, name: str):
        item = self._sources.get(name)
        return item[1] if item else None

    def backend(self, name: str):
        return self._backends.get(name)

    def provenance(self, probe_name: str) -> Optional[Dict[str, str]]:
        owner = self._probe_owner.get(probe_name)
        if not owner:
            return None
        record = self.records[owner]
        return {"plugin": record.name,
                "plugin_version": record.manifest.version,
                "plugin_digest": record.digest}

    def menu_extra(self) -> List[Dict[str, str]]:
        """Plugin identity goes into menu_version, so caches and signed
        envelopes reflect the capability set, not only the core probes."""
        return [{"name": r.name, "version": r.manifest.version,
                 "digest": r.digest} for r in self.records.values()]

    def source_names(self) -> List[str]:
        return sorted(self._sources)

    def backend_names(self) -> List[str]:
        return sorted(self._backends)

    def audit_sinks(self) -> List[Any]:
        return [sink for _owner, sink in self._sinks]

    def policy_hooks(self) -> List[Any]:
        return [hook for _owner, hook in self._hooks]

    def provider_names(self) -> List[str]:
        return sorted(self._providers)
