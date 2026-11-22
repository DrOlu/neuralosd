"""Plugin manifests — the declarative half of the plugin contract.

A plugin is code, so the manifest is deliberately explicit: what it is, which
plugin API it targets, which neuralOSd versions it supports, what kind(s) it
registers, how it runs, and what it is allowed to touch. The loader refuses a
manifest it cannot validate rather than guessing.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PLUGIN_SCHEMA = "neuralosd.plugin/1"
PLUGIN_API = "1"
KINDS = ("probes", "source", "model_provider", "backend", "audit_sink",
         "policy_hook")
RUNTIMES = ("inprocess", "process")
MANIFEST_NAME = "neuralosd-plugin.json"


class PluginError(Exception):
    """A plugin is invalid, incompatible, refused, or failed to load."""


def _req(value, kind, what):
    if not isinstance(value, kind):
        raise PluginError(f"manifest {what} must be {kind.__name__}")


@dataclass
class PluginManifest:
    name: str
    version: str
    plugin_api: str = PLUGIN_API
    kinds: List[str] = field(default_factory=list)
    entrypoint: Optional[str] = None
    runtime: str = "inprocess"
    command: List[str] = field(default_factory=list)
    permissions: Dict[str, Any] = field(default_factory=dict)
    config_schema: Dict[str, Any] = field(default_factory=dict)
    neuralosd: Optional[str] = None
    description: str = ""
    integrity: Optional[str] = None
    path: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": PLUGIN_SCHEMA, "name": self.name,
            "version": self.version, "plugin_api": self.plugin_api,
            "kinds": list(self.kinds), "entrypoint": self.entrypoint,
            "runtime": self.runtime, "command": list(self.command),
            "permissions": dict(self.permissions),
            "config_schema": dict(self.config_schema),
            "neuralosd": self.neuralosd, "description": self.description,
            "integrity": self.integrity,
        }


def validate_manifest(data: Dict[str, Any],
                      core_version: Optional[str] = None) -> PluginManifest:
    _req(data, dict, "root")
    if data.get("schema") not in (PLUGIN_SCHEMA, None):
        raise PluginError(f"unsupported manifest schema: {data.get('schema')!r}")
    name = data.get("name")
    version = data.get("version")
    if not isinstance(name, str) or not name.strip():
        raise PluginError("manifest name is required")
    if not isinstance(version, str) or not version.strip():
        raise PluginError("manifest version is required")
    api = str(data.get("plugin_api") or PLUGIN_API)
    kinds = data.get("kinds") or []
    _req(kinds, list, "kinds")
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise PluginError(f"unknown plugin kind(s): {', '.join(bad)}")
    runtime = data.get("runtime") or "inprocess"
    if runtime not in RUNTIMES:
        raise PluginError(f"unknown runtime: {runtime!r}")
    command = data.get("command") or []
    _req(command, list, "command")
    if runtime == "process" and not command:
        raise PluginError("a process plugin needs a command list")
    permissions = data.get("permissions") or {}
    _req(permissions, dict, "permissions")
    config_schema = data.get("config_schema") or {}
    _req(config_schema, dict, "config_schema")
    entrypoint = data.get("entrypoint")
    if runtime == "inprocess" and not entrypoint:
        raise PluginError("an in-process plugin needs an entrypoint")
    manifest = PluginManifest(
        name=name.strip(), version=version.strip(), plugin_api=api,
        kinds=list(kinds), entrypoint=entrypoint, runtime=runtime,
        command=[str(part) for part in command],
        permissions=permissions, config_schema=config_schema,
        neuralosd=data.get("neuralosd"),
        description=str(data.get("description") or ""),
        integrity=data.get("integrity"),
        path=data.get("path"))
    if core_version:
        check_compatible(manifest, core_version)
    return manifest


def _parse_range(text: str):
    """A tiny version-range parser for `>=3.6,<4`-style manifests."""
    from ..update import parse_version
    clauses = []
    for raw in str(text).replace(" ", "").split(","):
        if not raw:
            continue
        for op in (">=", "<=", "==", ">", "<", "!="):
            if raw.startswith(op):
                value = parse_version(raw[len(op):])
                if value is None:
                    raise PluginError(f"invalid version in range: {raw!r}")
                clauses.append((op, value))
                break
        else:
            raise PluginError(f"unsupported version range clause: {raw!r}")
    return clauses


def check_compatible(manifest: PluginManifest, core_version: str) -> None:
    if manifest.plugin_api.split(".")[0] != PLUGIN_API:
        raise PluginError(
            f"{manifest.name}: plugin API {manifest.plugin_api!r} is not "
            f"supported (this runtime implements {PLUGIN_API})")
    if not manifest.neuralosd:
        return
    from ..update import parse_version
    current = parse_version(core_version)
    if current is None:
        return
    for op, value in _parse_range(manifest.neuralosd):
        ok = {
            ">=": current >= value, "<=": current <= value,
            "==": current == value, ">": current > value,
            "<": current < value, "!=": current != value,
        }[op]
        if not ok:
            raise PluginError(
                f"{manifest.name}: requires neuralosd {manifest.neuralosd} "
                f"(this is {core_version})")


def load_manifest(source) -> PluginManifest:
    """Load a manifest from a path, a directory, or a dict."""
    from .. import __version__
    if isinstance(source, PluginManifest):
        check_compatible(source, __version__)
        return source
    if isinstance(source, dict):
        return validate_manifest(source, core_version=__version__)
    path = os.fspath(source)
    if os.path.isdir(path):
        manifest_path = os.path.join(path, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            raise PluginError(f"no {MANIFEST_NAME} in {path}")
        path = manifest_path
    if not os.path.isfile(path):
        raise PluginError(f"plugin manifest not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PluginError(f"cannot read {path}: {exc}") from exc
    manifest = validate_manifest(data, core_version=__version__)
    manifest.path = os.path.dirname(os.path.abspath(path))
    return manifest
