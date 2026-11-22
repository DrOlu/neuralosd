"""Load enabled plugins for one instance.

Discovery is explicit: an instance enables plugins in `plugins.json` (or the
caller passes `--plugin-dir`). Nothing is auto-loaded from the network, and a
plugin that is enabled but broken fails loudly rather than silently changing
the menu.
"""
import importlib
import importlib.util
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .api import PluginContext
from .manifest import (PluginError, PluginManifest, load_manifest,
                       tree_digest, verify_signature)
from .providers import fallback_from_provider, provider_from_spec
from .registry import Registry

CONFIG_NAME = "plugins.json"
LOCK_NAME = "plugins.lock"


@dataclass
class PluginBundle:
    registry: Registry = field(default_factory=Registry)
    config: Dict[str, Any] = field(default_factory=dict)

    def probes(self):
        return self.registry.probes()

    def provider(self, name: str):
        return self.registry.provider(name)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def plugin_config(instance_dir: str) -> Dict[str, Any]:
    data = _read_json(os.path.join(instance_dir, CONFIG_NAME))
    return data if isinstance(data, dict) else {}


def _load_module_from_dir(directory: str, name: str):
    path = os.path.join(directory, "plugin.py")
    if not os.path.isfile(path):
        raise PluginError(f"plugin module not found: {path}")
    spec = importlib.util.spec_from_file_location(
        f"neuralosd_plugin_{name.replace('.', '_')}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot import plugin module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_register(entry: Dict[str, Any], manifest: PluginManifest):
    if entry.get("path"):
        directory = os.path.abspath(os.path.expanduser(entry["path"]))
        module = _load_module_from_dir(directory, manifest.name)
        register = getattr(module, "register", None)
        if register is None:
            raise PluginError(f"{directory}/plugin.py has no register(ctx)")
        return register, directory
    spec = entry.get("entrypoint") or manifest.entrypoint
    if not spec or ":" not in spec:
        raise PluginError(
            f"{manifest.name}: entrypoint must be 'module:attribute'")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginError(f"{manifest.name}: cannot import {module_name}: "
                          f"{exc}") from exc
    register = getattr(module, attr, None)
    if register is None:
        raise PluginError(f"{manifest.name}: {module_name} has no {attr}")
    return register, os.path.dirname(getattr(module, "__file__", "") or "")


def _entry_manifest(entry: Dict[str, Any]) -> PluginManifest:
    if entry.get("path"):
        return load_manifest(entry["path"])
    if entry.get("manifest"):
        return load_manifest(entry["manifest"])
    spec = entry.get("entrypoint")
    if spec and ":" in spec:
        module_name = spec.split(":", 1)[0]
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise PluginError(f"cannot import {module_name}: {exc}") from exc
        manifest = getattr(module, "MANIFEST", None)
        if manifest is not None:
            return load_manifest(manifest)
        dist_meta = getattr(module, "__version__", "0")
        return load_manifest({
            "schema": "neuralosd.plugin/1",
            "name": entry.get("name") or module_name.rsplit(".", 1)[-1],
            "version": str(dist_meta), "plugin_api": "1",
            "kinds": entry.get("kinds") or ["probes"],
            "entrypoint": spec,
        })
    raise PluginError(f"plugin entry has no path/manifest/entrypoint: {entry}")


def _check_policy(instance_dir: str, manifest: PluginManifest) -> None:
    config = plugin_config(instance_dir)
    allow = config.get("allow")
    if isinstance(allow, list) and manifest.name not in allow:
        raise PluginError(
            f"{manifest.name} is not in plugins.json allow list")
    lock = _read_json(os.path.join(instance_dir, LOCK_NAME))
    if isinstance(lock, dict):
        locked = (lock.get("plugins") or {}).get(manifest.name)
        if locked:
            if locked.get("version") != manifest.version:
                raise PluginError(
                    f"{manifest.name}: locked version {locked.get('version')} "
                    f"does not match {manifest.version}")
            if locked.get("integrity"):
                actual = tree_digest(manifest.path or ".")
                if actual != locked["integrity"]:
                    raise PluginError(
                        f"{manifest.name}: integrity does not match "
                        f"plugins.lock (code changed since lock)")
    verify_signature(manifest)


def load_bundle(instance_dir: str, read_only: bool = False,
                plugin_dirs: Optional[List[str]] = None,
                extra_entries: Optional[List[Dict]] = None) -> PluginBundle:
    config = plugin_config(instance_dir)
    entries: List[Dict[str, Any]] = []
    for raw in config.get("plugins") or []:
        entry = {"path": raw} if isinstance(raw, str) else dict(raw)
        if entry.get("enabled", True):
            entries.append(entry)
    for directory in plugin_dirs or []:
        entries.append({"path": os.path.abspath(os.path.expanduser(directory))})
    entries.extend(extra_entries or [])
    bundle = PluginBundle(registry=Registry(), config=config)
    for entry in entries:
        manifest = _entry_manifest(entry)
        _check_policy(instance_dir, manifest)
        if manifest.runtime == "process":
            from .host import ProcessPlugin
            plugin = ProcessPlugin(manifest, config=entry.get("config") or {})
            record = plugin.record
            bundle.registry.add(record)
            continue
        register, directory = _resolve_register(entry, manifest)
        ctx = PluginContext(manifest, instance_dir,
                            config=entry.get("config") or {},
                            read_only=read_only)
        try:
            if isinstance(register, type):
                register().register(ctx)
            elif hasattr(register, "register") and not callable(register):
                register.register(ctx)
            else:
                register(ctx)
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PluginError(
                f"{manifest.name}: registration failed: "
                f"{type(exc).__name__}: {exc}") from exc
        manifest.path = manifest.path or directory
        bundle.registry.add(ctx.record)
    return bundle


def attach_provider(inst, spec: Optional[str] = None) -> None:
    """Select a model provider (CLI spec > plugins.json) and install it."""
    bundle = getattr(inst, "plugins", None)
    config = plugin_config(getattr(inst, "state_dir", ".") or ".")
    spec = spec or config.get("model_provider")
    if not spec:
        return
    provider = None
    if bundle is not None:
        provider = bundle.registry.provider(str(spec).split(":", 1)[0])
    if provider is None:
        provider = provider_from_spec(str(spec))
    inst.model_provider = provider
    inst.router.model_fallback = fallback_from_provider(provider)


def apply_to_instance(inst, instance_dir: str, model_provider: Optional[str] = None,
                      plugin_dirs: Optional[List[str]] = None,
                      read_only: bool = False) -> PluginBundle:
    bundle = load_bundle(instance_dir, read_only=read_only,
                         plugin_dirs=plugin_dirs)
    if bundle.probes():
        provenance = {}
        for fn in bundle.probes():
            meta = getattr(fn, "_probe", None)
            if meta is None:
                continue
            found = bundle.registry.provenance(meta.name)
            if found:
                provenance[meta.name] = found
        inst.add_probes(bundle.probes(), provenance=provenance)
    inst.plugins = bundle
    inst.router.audit_sinks = bundle.registry.audit_sinks()
    inst.router.policy_hooks = bundle.registry.policy_hooks()
    inst.plugin_provenance = {
        record.name: {"version": record.manifest.version,
                      "digest": record.digest}
        for record in bundle.registry.records.values()}
    attach_provider(inst, model_provider)
    return bundle
