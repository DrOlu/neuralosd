"""Packaging completeness: every loose (non-.py) file under src/neuralosd
must be shipped by the wheel.

Regression for v2.9.6: pi_durable_bridge.mjs existed in the source tree but
was never listed in [tool.setuptools.package-data], so every pip install
shipped a pidurable.py whose bridge script did not exist — `neuralosd build
--durable` died with a BrokenPipeError the moment node exited (module not
found). Catch any future loose file the same way: fail if a non-.py file
is neither covered by a package-data pattern nor listed in exclude-package-data.
"""
import fnmatch
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "neuralosd"


def _load_pyproject():
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11 dev envs
        import tomli as tomllib
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def _included(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(pathlib.PurePath(rel).name, pat)
               for pat in patterns)


def test_every_loose_file_is_packaged():
    pp = _load_pyproject()
    include = pp["tool"]["setuptools"]["package-data"]["neuralosd"]
    exclude = pp["tool"]["setuptools"].get("exclude-package-data", {}).get(
        "neuralosd", [])
    unshipped = []
    for p in SRC.rglob("*"):
        if not p.is_file() or p.suffix == ".py":
            continue
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(SRC).as_posix()
        if _included(rel, exclude):
            continue
        if not _included(rel, include):
            unshipped.append(rel)
    assert not unshipped, (
        "loose files exist under src/neuralosd but are NOT in "
        "[tool.setuptools.package-data] — they will be missing from every "
        f"pip install: {sorted(unshipped)}")


def test_durable_bridge_ships_next_to_pidurable():
    """The bridge must be importable-adjacent in an INSTALLED package.

    From a source checkout the file trivially exists; the real failure mode
    (v2.9.6) was a wheel without it. Assert the packaging declaration
    explicitly so this cannot silently regress even if the generic
    completeness test above is loosened."""
    pp = _load_pyproject()
    include = pp["tool"]["setuptools"]["package-data"]["neuralosd"]
    assert _included("pi_durable_bridge.mjs", include), (
        "pi_durable_bridge.mjs must be listed in package-data — pip "
        "installs ship no bridge without it (v2.9.6 defect)")


def test_bridge_command_stages_beside_the_node_modules(cleanup=None):
    """The bridge file's imports resolve from ITS OWN path, not the cwd.

    A pip-installed bridge (site-packages) can never import
    @earendil-works/* — v2.9.6-era BrokenPipeError. The launcher must stage
    a copy in <dir-of-node_modules>/.neuralosd-bridge/ so node's walk-up
    finds <dir>/node_modules/@earendil-works/*."""
    import os
    import shutil
    import tempfile
    from neuralosd import pidurable

    tmp = tempfile.mkdtemp(prefix="nosd-bridge-")
    try:
        pkg = os.path.join(tmp, "node_modules", "@earendil-works", "pi-durable")
        os.makedirs(pkg)
        argv = pidurable.pi_durable_command(
            "/tmp/x.sqlite", model="m", provider="p", cwd=tmp)
        assert argv[0] == "node"
        staged = argv[1]
        assert os.path.sep + ".neuralosd-bridge" + os.path.sep in staged, staged
        assert os.path.isfile(staged), "staged bridge was not materialised"
        with open(staged, encoding="utf-8") as f1, \
                open(pidurable.bridge_script_path(), encoding="utf-8") as f2:
            assert f1.read() == f2.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bridge_command_falls_back_to_packaged_path_without_node_modules():
    import os
    import tempfile
    from neuralosd import pidurable

    tmp = tempfile.mkdtemp(prefix="nosd-bridge-none-")
    try:
        argv = pidurable.pi_durable_command(
            "/tmp/x.sqlite", model="m", provider="p",
            cwd=os.path.join(tmp, "deep", "nested"))
        assert argv[1] == pidurable.bridge_script_path()
    finally:
        os.rmdir(tmp)


def test_stale_staged_bridge_is_refreshed():
    import os
    import shutil
    import tempfile
    import time
    from neuralosd import pidurable

    tmp = tempfile.mkdtemp(prefix="nosd-bridge-stale-")
    try:
        pkg = os.path.join(tmp, "node_modules", "@earendil-works", "pi-durable")
        os.makedirs(pkg)
        argv1 = pidurable.pi_durable_command("/x", cwd=tmp)
        staged = argv1[1]
        with open(staged, "a", encoding="utf-8") as f:
            f.write("\n// corrupted local edit")
        time.sleep(0.02)
        argv2 = pidurable.pi_durable_command("/x", cwd=tmp)
        assert argv2[1] == staged
        with open(staged, encoding="utf-8") as f:
            assert "// corrupted local edit" not in f.read(), \
                "stale/corrupted stage was not refreshed from the packaged copy"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
