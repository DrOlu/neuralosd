#!/usr/bin/env python3
"""Detect and record drift in neuralosd's integrated components.

neuralosd is a composition: the on-device model (`neuralos`), two sandbox
backends (`boxlite`, `microsandbox`), and the data/binary toolchain. When any
of them publishes a new version, neuralosd should follow.

There is no "package X was released" webhook, so this polls PyPI and compares
against a recorded snapshot (`components.json`, committed to the repo). The
snapshot is the source of truth: it makes the update auditable and means
re-running without changes is a no-op.

Usage:
    component_versions.py --check          # report drift, write $GITHUB_OUTPUT
    component_versions.py --record         # snapshot current versions
    component_versions.py --bump           # bump neuralosd's patch version

Exit codes: 0 = fine (with or without drift), 1 = an error occurred.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Every component neuralosd composes. `scope` decides what a change triggers:
#   runtime -> rebuild the binaries AND publish a new PyPI release
#   build   -> rebuild the binaries only (no runtime behaviour changes)
COMPONENTS: dict[str, dict[str, str]] = {
    "neuralos": {
        "scope": "runtime",
        "why": "the on-device model runtime (needle engine + weights)",
    },
    "boxlite": {
        "scope": "runtime",
        "why": "BoxLite sandbox backend",
    },
    "microsandbox": {
        "scope": "runtime",
        "why": "Microsandbox sandbox backend (bundles the msb CLI)",
    },
    "pydantic": {
        "scope": "runtime",
        "why": "validation models for the [data] extra",
    },
    "pymysql": {
        "scope": "runtime",
        "why": "MySQL driver for the [data] extra",
    },
    "openpyxl": {
        "scope": "runtime",
        "why": "Excel reader — baked into every binary",
    },
    "nuitka": {
        "scope": "build",
        "why": "the compiler that produces the standalone binaries",
    },
}

# A version containing any of these is a pre-release and is ignored by default:
# auto-publishing betas of a dependency is rarely what anyone wants.
PRERELEASE = re.compile(r"(a|b|rc|alpha|beta|dev|pre)\d*$", re.I)

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO_ROOT / "components.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "neuralosd" / "__init__.py"


# ── version helpers ────────────────────────────────────────────────────────

def is_prerelease(version: str) -> bool:
    return bool(PRERELEASE.search(version or ""))


def latest_version(name: str, timeout: float = 20.0) -> str | None:
    """Newest version of `name` on PyPI, or None if it cannot be determined."""
    try:
        with urllib.request.urlopen(PYPI_JSON.format(name=name),
                                    timeout=timeout) as fh:
            data = json.load(fh)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return (data.get("info") or {}).get("version")


def load_snapshot(path: Path | None = None) -> dict:
    path = Path(path) if path else SNAPSHOT
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_snapshot(versions: dict, path: Path | None = None) -> None:
    path = Path(path) if path else SNAPSHOT
    payload = {
        "_comment": ("Recorded versions of the components neuralosd composes. "
                     "Updated automatically by "
                     ".github/workflows/auto-update.yml when an upstream "
                     "release is detected."),
        "components": {k: versions.get(k, "") for k in COMPONENTS},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def check(allow_prerelease: bool = False) -> dict:
    """Compare PyPI against the snapshot. Returns a report dict."""
    recorded = load_snapshot().get("components", {})
    rows, unknown, changed_runtime, changed_build = [], [], [], []

    for name, meta in COMPONENTS.items():
        old = recorded.get(name, "")
        new = latest_version(name)
        if new is None:
            status = "unknown"
            unknown.append(name)
        elif not old:
            status = "new"            # first time we look at it
        elif new == old:
            status = "same"
        elif is_prerelease(new) and not allow_prerelease:
            status = "prerelease-skipped"
        else:
            status = "UPDATE"
            (changed_runtime if meta["scope"] == "runtime"
             else changed_build).append(name)
        rows.append({"name": name, "recorded": old, "latest": new or "?",
                     "status": status, "scope": meta["scope"],
                     "why": meta["why"], "resolved": new})

    needs_record = (not recorded) or any(r["status"] == "new" for r in rows)
    return {
        "components": rows,
        "unknown": unknown,
        "needs_record": needs_record,
        "changed": bool(changed_runtime or changed_build),
        "runtime_changed": changed_runtime,
        "build_changed": changed_build,
        "resolved": {r["name"]: (r["resolved"] or r["recorded"])
                     for r in rows},
    }


# ── rendering ──────────────────────────────────────────────────────────────

def table(report: dict) -> str:
    icon = {"same": "  ", "UPDATE": "⬆️", "new": "＋",
            "unknown": "❔", "prerelease-skipped": "⏭️"}
    lines = ["| component | recorded | latest | scope | status |",
             "|---|---|---|---|---|"]
    for r in report["components"]:
        lines.append(f"| `{r['name']}` | {r['recorded'] or '—'} | "
                     f"{r['latest']} | {r['scope']} | "
                     f"{icon.get(r['status'], '')} {r['status']} |")
    return "\n".join(lines)


def write_outputs(report: dict, path: str | None = None) -> None:
    """Write GitHub Actions outputs (no-op outside Actions)."""
    path = path or os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    def g(key, value):
        return f"{key}={value}\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(g("changed", str(report["changed"]).lower()))
        fh.write(g("needs_record",
                   str(bool(report.get("needs_record"))).lower()))
        fh.write(g("runtime_changed",
                   str(bool(report["runtime_changed"])).lower()))
        fh.write(g("build_changed", ",".join(report["build_changed"])))
        fh.write(g("components", ",".join(report["runtime_changed"])))
        fh.write("summary<<EOF\n" + table(report) + "\nEOF\n")


# ── version bumping ────────────────────────────────────────────────────────

def current_version(pyproject: Path | None = None) -> str:
    pyproject = Path(pyproject) if pyproject else PYPROJECT
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(
        encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("could not read version from pyproject.toml")
    return m.group(1)


def bump_version(part: str = "patch", pyproject: Path | None = None,
                 init_py: Path | None = None) -> tuple[str, str]:
    """Increment the version in pyproject.toml and __init__.py."""
    pyproject = Path(pyproject) if pyproject else PYPROJECT
    init_py = Path(init_py) if init_py else INIT_PY
    old = current_version(pyproject)
    nums = re.findall(r"\d+", old)[:3]
    while len(nums) < 3:
        nums.append("0")
    major, minor, patch = (int(n) for n in nums)
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    new = f"{major}.{minor}.{patch}"

    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        re.sub(r'^(version\s*=\s*")[^"]+(")', rf"\g<1>{new}\g<2>", text,
               count=1, flags=re.M), encoding="utf-8")

    if init_py.is_file():
        body = init_py.read_text(encoding="utf-8")
        init_py.write_text(body.replace(old, new), encoding="utf-8")
    return old, new


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--check", action="store_true",
                   help="report drift against components.json")
    g.add_argument("--bump", metavar="PART", nargs="?", const="patch",
                   choices=["patch", "minor", "major"],
                   help="bump the neuralosd version (default: patch)")
    ap.add_argument("--record", action="store_true",
                    help="with --check: write the snapshot afterwards")
    ap.add_argument("--allow-prerelease", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    a = ap.parse_args(argv)

    if a.record and not a.check:
        print("error: --record must be combined with --check", file=sys.stderr)
        return 2
    if not a.check and not a.bump:
        print("error: one of --check or --bump is required", file=sys.stderr)
        return 2

    if a.bump:
        old, new = bump_version(a.bump)
        print(f"{old} -> {new}")
        path = os.environ.get("GITHUB_OUTPUT")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"old_version={old}\nnew_version={new}\n")
        return 0

    report = check(allow_prerelease=a.allow_prerelease)

    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"neuralosd {current_version()} — component drift")
        print(table(report))
        if report["unknown"]:
            print(f"\n⚠️  could not reach PyPI for: "
                  f"{', '.join(report['unknown'])}")
        if report["runtime_changed"]:
            print(f"\nruntime change: {', '.join(report['runtime_changed'])}")
        if report["build_changed"]:
            print(f"build change:   {', '.join(report['build_changed'])}")
        if not report["changed"]:
            print("\nno drift — nothing to update")
        if report.get("needs_record"):
            print("snapshot is missing or incomplete — record it")

    if a.record:
        save_snapshot({**load_snapshot().get("components", {}),
                       **report["resolved"]})
        print(f"\nrecorded to {SNAPSHOT.name}")
    else:
        write_outputs(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
