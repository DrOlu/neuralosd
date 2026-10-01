"""`neuralosd diff` — schema-evolution diff between two menus/instances."""
import json
import os


def _load_menu(path):
    if os.path.isdir(path):
        from ._common import load_instance
        return load_instance(path).menu
    with open(path) as f:
        data = json.load(f)
    return data.get("menu", data) if isinstance(data, dict) else data


def run(a):
    old = _load_menu(a.old)
    new = _load_menu(a.new)
    old_by = {m["name"]: m for m in old}
    new_by = {m["name"]: m for m in new}

    added = sorted(set(new_by) - set(old_by))
    removed = sorted(set(old_by) - set(new_by))
    changed = []
    for name in sorted(set(old_by) & set(new_by)):
        o, n = old_by[name], new_by[name]
        deltas = {}
        if o.get("triggers") != n.get("triggers"):
            deltas["triggers"] = {"old": o.get("triggers"), "new": n.get("triggers")}
        if o.get("parameters") != n.get("parameters"):
            deltas["parameters"] = {"old": o.get("parameters"),
                                    "new": n.get("parameters")}
        if o.get("description") != n.get("description"):
            deltas["description"] = {"old": o.get("description"),
                                     "new": n.get("description")}
        if deltas:
            changed.append({"name": name, "changes": deltas})

    out = {"added": added, "removed": removed, "changed": changed}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    raise SystemExit(1 if (added or removed or changed) else 0)