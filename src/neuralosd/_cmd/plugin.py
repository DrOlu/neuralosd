"""`neuralosd plugin` — discover, enable, test and lock plugins.

    neuralosd plugin list
    neuralosd plugin inspect ./my-plugin
    neuralosd plugin enable acme --instance-dir ./inst --path ./acme-plugin
    neuralosd plugin test ./acme-plugin
    neuralosd plugin new my-probes --kind probes --out ./my-probes
    neuralosd plugin lock --instance-dir ./inst

A plugin is enabled per instance in `plugins.json`; an enabled but broken
plugin fails loudly, because a silently missing capability is worse.
"""
import hashlib
import json
import os
import shutil
import sys

from ..plugins.loader import CONFIG_NAME, LOCK_NAME, load_bundle, plugin_config
from ..plugins.manifest import (MANIFEST_NAME, PluginError, load_manifest)


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _instance_config(instance_dir, create=False):
    path = os.path.join(instance_dir, CONFIG_NAME)
    config = plugin_config(instance_dir)
    if not config and create:
        config = {"plugins": []}
    config.setdefault("plugins", [])
    return path, config


def _find_entry(config, name):
    for entry in config.get("plugins") or []:
        if isinstance(entry, str):
            if os.path.basename(entry.rstrip("/")) == name:
                return entry
        elif entry.get("name") == name or (
                entry.get("path")
                and os.path.basename(str(entry["path"]).rstrip("/")) == name):
            return entry
    return None


def _tree_digest(directory):
    h = hashlib.sha256()
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, directory)
            h.update(rel.encode())
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
    return "sha256:" + h.hexdigest()


def run(a):
    cmd = getattr(a, "plugin_cmd", None) or "list"
    instance_dir = getattr(a, "instance_dir", None)
    as_json = bool(getattr(a, "json", False))

    try:
        if cmd == "list":
            rows = []
            if instance_dir:
                config = plugin_config(instance_dir)
                for entry in config.get("plugins") or []:
                    rows.append(entry if isinstance(entry, dict)
                                else {"path": entry})
            try:
                from importlib.metadata import entry_points
                for ep in entry_points(group="neuralosd.plugins"):
                    rows.append({"entrypoint": ep.value, "name": ep.name,
                                 "installed": True})
            except Exception:
                pass
            if as_json:
                print(json.dumps({"plugins": rows}, indent=2))
            else:
                for row in rows:
                    print(f"{row.get('name', '-'):24s} "
                          f"{row.get('path') or row.get('entrypoint')}")
                if not rows:
                    print("no plugins enabled or installed")
            return 0

        if cmd == "inspect":
            manifest = load_manifest(getattr(a, "target"))
            print(json.dumps(manifest.to_dict(), indent=2))
            return 0

        if cmd == "enable":
            if not instance_dir:
                print("error: enable needs --instance-dir", file=sys.stderr)
                return 2
            path = getattr(a, "path", None)
            if not path:
                print("error: enable needs --path <plugin directory>",
                      file=sys.stderr)
                return 2
            manifest = load_manifest(path)
            cfg_path, config = _instance_config(instance_dir, create=True)
            entry = _find_entry(config, manifest.name)
            new = {"name": manifest.name, "path": os.path.abspath(path),
                   "enabled": True, "config": {}}
            if entry is not None:
                config["plugins"][config["plugins"].index(entry)] = new
            else:
                config["plugins"].append(new)
            _write_json(cfg_path, config)
            print(f"enabled {manifest.name} for {instance_dir}")
            return 0

        if cmd == "disable":
            if not instance_dir:
                print("error: disable needs --instance-dir", file=sys.stderr)
                return 2
            cfg_path, config = _instance_config(instance_dir, create=True)
            entry = _find_entry(config, getattr(a, "name"))
            if entry is None:
                print(f"error: no plugin {a.name!r} in {cfg_path}",
                      file=sys.stderr)
                return 1
            if isinstance(entry, dict):
                entry["enabled"] = False
            else:
                config["plugins"].remove(entry)
            _write_json(cfg_path, config)
            print(f"disabled {a.name} for {instance_dir}")
            return 0

        if cmd == "test":
            target = getattr(a, "target")
            manifest = load_manifest(target)
            host_dir = target if os.path.isdir(target) else os.path.dirname(
                os.path.abspath(target))
            bundle = load_bundle(host_dir,
                                 extra_entries=[{"path": target}])
            record = bundle.registry.records.get(manifest.name)
            if record is None:
                raise PluginError("plugin registered nothing")
            probes = [p._probe.name for p in record.probes]
            print(f"OK {manifest.name} {manifest.version}: "
                  f"{len(probes)} probe(s), {len(record.providers)} "
                  f"provider(s), {len(record.sources)} source(s), "
                  f"{len(record.backends)} backend(s)")
            for name in probes:
                print(f"  probe {name}")
            return 0

        if cmd == "new":
            kind = getattr(a, "kind", "probes")
            out = os.path.abspath(getattr(a, "out") or a.name)
            os.makedirs(out, exist_ok=True)
            manifest = {
                "schema": "neuralosd.plugin/1", "name": a.name,
                "version": "0.1.0", "plugin_api": "1", "kinds": [kind],
                "entrypoint": "plugin:register", "runtime": "inprocess",
                "description": f"{a.name} plugin",
                "permissions": {}, "neuralosd": ">=3.6,<4",
            }
            body = ('"""Generated by `neuralosd plugin new`."""\n'
                    "from neuralosd import probe\n"
                    "from neuralosd.plugins.providers import ModelProvider\n\n\n")
            if kind == "model_provider":
                body += (
                    "class Provider(ModelProvider):\n"
                    "    name = %r\n\n"
                    "    def select(self, question, menu, k=8):\n"
                    "        return None   # abstain\n\n\n"
                    "def register(ctx):\n"
                    "    ctx.register_provider(%r, Provider())\n"
                    % (a.name, a.name))
            else:
                body += (
                    "@probe(description=%r, triggers=[%r])\n"
                    "def hello():\n"
                    "    return {\"hello\": \"from %s\"}\n\n\n"
                    "def register(ctx):\n"
                    "    ctx.register_probe(hello)\n"
                    % (f"{a.name} hello", f"{a.name} hello", a.name))
            with open(os.path.join(out, MANIFEST_NAME), "w",
                      encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
                fh.write("\n")
            with open(os.path.join(out, "plugin.py"), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
            print(f"created {out} ({kind})")
            print(f"  test it: neuralosd plugin test {out}")
            return 0

        if cmd == "sources":
            names = []
            if instance_dir:
                names = load_bundle(instance_dir).registry.source_names()
            print("\n".join(names) or "no sources")
            return 0

        if cmd == "providers":
            print("\n".join(["deterministic", "ollama", "openai"]))
            return 0

        if cmd == "doctor":
            if not instance_dir:
                print("error: doctor needs --instance-dir", file=sys.stderr)
                return 2
            bundle = load_bundle(instance_dir)
            print(json.dumps({
                "plugins": list(bundle.registry.records),
                "probes": [p._probe.name for p in bundle.probes()],
                "providers": bundle.registry.provider_names(),
                "backends": bundle.registry.backend_names(),
            }, indent=2))
            return 0

        if cmd == "lock":
            if not instance_dir:
                print("error: lock needs --instance-dir", file=sys.stderr)
                return 2
            config = plugin_config(instance_dir)
            locked = {}
            for entry in config.get("plugins") or []:
                path = entry.get("path") if isinstance(entry, dict) else entry
                if not path:
                    continue
                manifest = load_manifest(path)
                locked[manifest.name] = {
                    "version": manifest.version,
                    "integrity": _tree_digest(path),
                }
            _write_json(os.path.join(instance_dir, LOCK_NAME),
                        {"schema": "neuralosd.plugins.lock/1",
                         "plugins": locked})
            print(f"locked {len(locked)} plugin(s) in {instance_dir}")
            return 0

        if cmd == "install":
            source = os.path.abspath(getattr(a, "target"))
            manifest = load_manifest(source)
            dest_root = os.path.expanduser("~/.neuralosd/plugins")
            dest = os.path.join(dest_root, manifest.name)
            os.makedirs(dest_root, exist_ok=True)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(source, dest,
                            ignore=shutil.ignore_patterns("__pycache__"))
            if manifest.integrity:
                actual = _tree_digest(dest)
                if actual != manifest.integrity:
                    raise PluginError(
                        f"integrity mismatch: {actual} != {manifest.integrity}")
            print(f"installed {manifest.name} -> {dest}")
            print(f"  enable it: neuralosd plugin enable {manifest.name} "
                  f"--instance-dir <DIR> --path {dest}")
            return 0

        print(f"error: unknown plugin command {cmd}", file=sys.stderr)
        return 2
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
