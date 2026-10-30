"""Instance import audit — close the "frozen binary can't see instance
imports" gap generically.

The frozen binary loads each instance's bridge.py/instance.py/probes.py
dynamically at runtime; its build cannot know what those files import. Any
module the binary does not bundle (and the sidecar does not provide) fails
at load or probe time — first seen with stdlib `_sqlite3` (fixed by
force-including it in v2.0.2), then with third-party `cryptography`.

This module is the *detection* half of the durable fix: AST-parse every
instance module, collect the real imports (top-level AND function-level),
and classify each against (a) the frozen runtime's payload and (b) the
host/sidecar python. `neuralosd audit` wires it into the instance-creation
flow so an under-provisioned instance is caught before it ships.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Set

INSTANCE_MODULES = ("bridge", "instance", "probes")

BUNDLED = "bundled"                # importable inside the frozen runtime
HOST_ONLY = "host-only"            # not bundled, but the sidecar/host has it
INSTANCE_LOCAL = "instance-local"  # a sibling .py/package in the instance dir
MISSING_EVERYWHERE = "missing-everywhere"   # neither — instance WILL fail

# NOTE: there is deliberately NO "known-bundled" fallback list here. A module
# counts as bundled only when find_spec proved it in the actual runtime — an
# assumption is exactly the failure mode this module exists to prevent.


# ── collection ──────────────────────────────────────────────────────────────
def collect_imports(path: str) -> Set[str]:
    """Every top-level module name imported anywhere in `path`.

    AST-based (not regex): catches top-level AND function-level imports,
    `import a.b` → 'a', `from a.b import c` → 'a', try/except ImportError
    fallbacks, and aliases. Relative imports (level > 0) are skipped — they
    resolve inside the instance dir, not the runtime.
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    mods: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def instance_module_paths(instance_dir: str) -> List[str]:
    """bridge.py / instance.py / probes.py that actually exist."""
    return [os.path.join(instance_dir, m + ".py")
            for m in INSTANCE_MODULES
            if os.path.isfile(os.path.join(instance_dir, m + ".py"))]


def instance_local_modules(instance_dir: str) -> Set[str]:
    """Module names that resolve INSIDE the instance directory: sibling .py
    files (models.py, graph_bridge.py, ...) and local packages (dir with
    __init__.py). The adapter puts the dir on sys.path before importing the
    bridge, so these ALWAYS resolve at runtime — they must never be reported
    as missing-everywhere (v2.0.4 false-positive bug).
    """
    names: Set[str] = set()
    try:
        entries = os.listdir(instance_dir)
    except OSError:
        return names
    for entry in entries:
        if entry.endswith(".py") and entry != "__init__.py":
            names.add(entry[:-3])
        else:
            full = os.path.join(instance_dir, entry)
            if os.path.isdir(full) and os.path.isfile(
                    os.path.join(full, "__init__.py")):
                names.add(entry)
    return names


def collect_instance_imports(instance_dir: str) -> Set[str]:
    mods: Set[str] = set()
    for path in instance_module_paths(instance_dir):
        mods |= collect_imports(path)
    # instance-internal cross-imports (probes.py doing `import bridge`) resolve
    # inside the instance dir — the adapter puts it on sys.path — so they can
    # never be a runtime gap. Only imports the RUNTIME must satisfy remain.
    return mods - set(INSTANCE_MODULES)


# ── empirical probing ───────────────────────────────────────────────────────
def importable_set(exe: str, modules: Iterable[str],
                   timeout: int = 60) -> Set[str]:
    """Which of `modules` can `exe` actually import? Empirical, not assumed:
    one subprocess, find_spec per module (no side effects from importing)."""
    mods = sorted(set(modules))
    if not mods:
        return set()
    code = (
        "import importlib.util, json, sys\n"
        f"mods = json.loads({json.dumps(json.dumps(mods))})\n"
        "print(json.dumps([m for m in mods "
        "if importlib.util.find_spec(m) is not None]))\n"
    )
    try:
        out = subprocess.run([exe, "-c", code], capture_output=True,
                             text=True, timeout=timeout)
        if out.returncode == 0:
            return set(json.loads(out.stdout.strip() or "[]"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return set()


def current_runtime_importable(modules: Iterable[str]) -> Set[str]:
    """find_spec in THIS runtime (the frozen payload when run by the binary)."""
    ok = set()
    for m in set(modules):
        try:
            if importlib_util_find_spec(m) is not None:
                ok.add(m)
        except (ImportError, ValueError):
            pass
    return ok


def importlib_util_find_spec(name: str):
    import importlib.util
    return importlib.util.find_spec(name)


def sidecar_python() -> Optional[str]:
    """The host/sidecar python the binary should delegate to.

    Order: NEURALOSD_SIDECAR env (set host-wide, per the sidecar runbook) →
    the real host python when running frozen → None.
    """
    env = os.environ.get("NEURALOSD_SIDECAR")
    if env and os.path.isfile(env):
        return env
    if env and os.path.isdir(env) and os.path.isfile(
            os.path.join(env, "python.exe")):
        return os.path.join(env, "python.exe")
    if getattr(sys, "frozen", False):
        return None            # frozen: only an explicitly-configured sidecar counts
    return sys.executable      # dev/pip runtime: we ARE the host python


# ── classification + report ─────────────────────────────────────────────────
def classify_imports(modules: Iterable[str], bundled: Set[str],
                     host: Optional[Set[str]],
                     instance_local: Optional[Set[str]] = None) -> Dict[str, str]:
    """module → bundled | host-only | instance-local | missing-everywhere.

    `bundled` = importable in the frozen payload (EMPIRICAL — find_spec in
    that runtime, never assumed); `host` = importable by the sidecar/host
    python (None = unknown → anything unproven is reported as
    missing-everywhere, never silently passed). `instance_local` = siblings
    that live in the instance dir and resolve via its sys.path entry —
    satisfied by construction. Genuine stdlib with an unknown host is
    flagged host-only, since the payload ships most of the stdlib subset it
    was built with — but always flagged, never 'bundled' without proof.
    """
    local = instance_local or set()
    out: Dict[str, str] = {}
    for m in sorted(set(modules)):
        if m in local:
            out[m] = INSTANCE_LOCAL
        elif m in bundled:
            out[m] = BUNDLED
        elif host is not None and m in host:
            out[m] = HOST_ONLY
        elif host is None and _stdlib(m):
            out[m] = HOST_ONLY
        else:
            out[m] = MISSING_EVERYWHERE
    return out


def _stdlib(name: str) -> bool:
    try:
        import sys as _s
        return name in getattr(_s, "stdlib_module_names", ())
    except Exception:                                   # noqa: BLE001
        return False


def audit_instance(instance_dir: str, bundled: Set[str],
                   host: Optional[Set[str]]) -> Dict:
    mods = collect_instance_imports(instance_dir)
    classes = classify_imports(mods, bundled, host,
                               instance_local=instance_local_modules(instance_dir))
    missing = sorted(m for m, c in classes.items() if c == MISSING_EVERYWHERE)
    return {"instance": os.path.basename(os.path.abspath(instance_dir)),
            "dir": instance_dir,
            "imports": classes,
            "missing_everywhere": missing,
            "ok": not missing}


def audit_root(root: str, host_exe: Optional[str] = None) -> Dict:
    """Audit every instance under `root` (any dir holding needle_menu.json)."""
    instances = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if "needle_menu.json" in filenames:
            instances.append(dirpath)
    runtime = current_runtime_importable(
        set().union(*(collect_instance_imports(i) for i in instances)) or set())
    # the frozen payload is the empirical truth: whatever find_spec resolves
    # in THIS runtime is bundled, by definition — no assumptions needed
    bundled = runtime
    host = None
    if not getattr(sys, "frozen", False):
        host = set(bundled)              # dev/pip runtime: host == runtime
    elif host_exe:
        all_mods = set().union(*(collect_instance_imports(i) for i in instances)) or set()
        host = importable_set(host_exe, all_mods)
    reports = [audit_instance(i, bundled, host) for i in sorted(instances)]
    return {"root": root, "runtime": "frozen" if getattr(sys, "frozen", False)
            else "pip", "host_python": host_exe, "instances": reports,
            "ok": all(r["ok"] for r in reports)}


def print_report(report: Dict) -> str:
    lines = []
    for r in report["instances"]:
        status = "OK" if r["ok"] else "WILL FAIL"
        lines.append(f"{r['instance']:<40} [{status}]")
        for mod, cls in sorted(r["imports"].items()):
            mark = {"bundled": "+", "host-only": "~", "instance-local": "·",
                    "missing-everywhere": "✗"}[cls]
            lines.append(f"  {mark} {mod:<28} {cls}")
        if r["missing_everywhere"]:
            lines.append(f"  → fix: pip install "
                         f"{' '.join(r['missing_everywhere'])} into the "
                         f"sidecar, or set NEURALOSD_SIDECAR")
    return "\n".join(lines)


def to_json(report: Dict) -> str:
    return json.dumps(report, indent=2)
