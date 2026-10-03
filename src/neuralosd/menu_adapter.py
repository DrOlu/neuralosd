"""Adapter: serve a classic needle-menu instance from the neuralosd router.

The chinook instance (and every instance built before neuralosd) is a
`needle_menu.json` plus a module of callables — no `probes.py`. Rather than
require a rewrite, this lifts the existing menu into `ProbeMeta` objects so the
same router, the same argument caging and the same accounting apply.

Nothing is re-implemented: the menu's own `parameters` (enum values, regex
patterns, defaults) become the caged args, and each callable is invoked exactly
as the old engine invoked it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

MENU_FILE = "needle_menu.json"
DERIVED_FILE = "derived.json"


def _load_module(path: str, name: str):
    """Import a sibling module by path, without polluting sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None, f"no import spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                            # noqa: BLE001
        sys.modules.pop(name, None)
        return None, f"{type(e).__name__}: {e}"
    return mod, None


def read_menu(instance_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(instance_dir, MENU_FILE)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    menu = blob.get("menu", blob) if isinstance(blob, dict) else blob
    return [e for e in menu if isinstance(e, dict) and e.get("name")]


def args_from_entry(entry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The menu's declared parameters, as the router's arg specs.

    A parameter that is not in the entry's `required` list is optional here,
    which matters: optional args fall back to their declared default instead of
    making the probe unroutable.
    """
    params = entry.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    args: Dict[str, Dict[str, Any]] = {}
    for aname, spec in props.items():
        if not isinstance(spec, dict):
            continue
        s = {k: v for k, v in spec.items() if k in
             ("type", "enum", "values", "pattern", "min", "max",
              "description", "default")}
        # the menu carries enums as {"type":"string","enum":[...]}
        if "enum" in s and "values" not in s:
            s["values"] = s.pop("enum")
        if "values" in s and "type" not in s:
            s["type"] = "enum"
        if aname not in required:
            s["required"] = False
        args[aname] = s
    return args


def probes_from_menu(instance_dir: str,
                     module_names: Optional[List[str]] = None,
                     errors: Optional[Dict[str, str]] = None) -> List[Callable]:
    """Every menu entry that has a callable behind it, as a routable probe.

    Pass `errors` (a dict) to collect why a module failed to import. Without it,
    a database driver that is not installed looks exactly like a menu whose
    names do not match its functions — and you go looking in the wrong place.
    """
    from .probe import ProbeMeta

    menu = read_menu(instance_dir)
    if not menu:
        return []
    if instance_dir not in sys.path:
        sys.path.insert(0, instance_dir)

    mods = []
    for name in (module_names or ["bridge", "instance", "probes"]):
        path = os.path.join(instance_dir, name + ".py")
        if not os.path.isfile(path):
            continue
        m, err = _load_module(
            path, f"_nsk_{os.path.basename(instance_dir)}_{name}")
        if m is not None:
            mods.append(m)
        elif errors is not None:
            errors[name] = err

    out: List[Callable] = []
    for entry in menu:
        name = entry["name"]
        fn = next((getattr(m, name) for m in mods
                   if callable(getattr(m, name, None))), None)
        if fn is None:
            continue
        args = args_from_entry(entry)
        triggers = [str(t) for t in (entry.get("triggers") or [])]

        def make(orig=fn):
            def wrapper(**kwargs):
                return orig(**kwargs)
            return wrapper

        w = make()
        w.__name__ = name
        # Attach the meta directly: the menu already IS the declaration, and
        # re-decorating would risk reinterpreting its argument specs.
        w._probe = ProbeMeta(
            name=name,
            description=str(entry.get("description") or name),
            triggers=triggers,
            args=args,
            pii=list(entry.get("pii") or []),
            conf_gate=entry.get("conf_gate"),
            tier="in-process",
            confirm=bool(entry.get("confirm")),
            function=w)
        out.append(w)
    return out


def derived_probes(instance_dir: str,
                   observations: Optional[Dict[str, Dict]] = None
                   ) -> List[Callable]:
    """Load derived.json and turn each metric into a routable probe."""
    from .derived import as_probe, load as load_derived

    path = os.path.join(instance_dir, DERIVED_FILE)
    metrics = load_derived(path)
    if not metrics:
        return []
    if observations is None:
        observations = observe(instance_dir)
    out = []
    for m in metrics:
        p = as_probe(m, observations)
        if p is not None:
            out.append(p)
    return out


def observe(instance_dir: str,
            module_names: Optional[List[str]] = None) -> Dict[str, Dict]:
    """Call every probe once and record its output.

    This is what makes the closed inventory TRUE: every id a model may choose
    comes from something a probe really returned.

    Handles BOTH instance shapes — a classic needle menu, and a generated
    `probes.py`. The menu path is tried first because it is the only one that
    can be read without importing the data layer.
    """
    from .derived import is_derived
    from .reasoning import observations_from

    probes = probes_from_menu(instance_dir, module_names)
    if not probes:
        from ._cmd._common import load_instance
        try:
            probes = list(load_instance(instance_dir).probes)
        except SystemExit:
            return {}
    return observations_from([p for p in probes if not is_derived(p)])


def load_menu_instance(instance_dir: str):
    """Build an `Instance` from a needle_menu.json directory."""
    from .instance import Instance

    probes = probes_from_menu(instance_dir)
    if not probes:
        return None
    probes = probes + derived_probes(instance_dir)
    name = os.path.basename(os.path.abspath(instance_dir).rstrip(os.sep))
    return Instance(name=name, probes=probes, state_dir=instance_dir)
