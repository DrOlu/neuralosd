"""The neuralOSd plugin system.

Five kinds (probe packs, sources, model providers, sandbox backends, audit
sinks/policy hooks) share one versioned manifest and one registry. The core
keeps ownership of the refusal gate, the signed envelope and the audit chain:
plugins may add capabilities, never bypass those.
"""
from .manifest import (KINDS, MANIFEST_NAME, PLUGIN_API, PLUGIN_SCHEMA,
                       PluginError, PluginManifest, check_compatible,
                       load_manifest, validate_manifest)
from .registry import PluginRecord, Registry

__all__ = [
    "KINDS", "MANIFEST_NAME", "PLUGIN_API", "PLUGIN_SCHEMA",
    "PluginError", "PluginManifest", "PluginRecord", "Registry",
    "check_compatible", "load_manifest", "validate_manifest",
]
