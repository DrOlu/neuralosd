"""Menu-instance diagnostics: sqlite3 in frozen builds, surfaced import errors.

Background: Nuitka cannot see the imports of dynamically-loaded instance
modules (bridge.py / instance.py), so a frozen Windows binary once shipped
without the sqlite3 stdlib module — bridges doing `import sqlite3` failed
with a misleading "declares no probe that a callable actually backs" hint.
"""

import json
import os
import subprocess
import sys

import pytest

from neuralosd.menu_adapter import load_menu_instance, probes_from_menu

MENU = [{"name": "x_count", "description": "Count rows in the demo table",
         "triggers": ["how many x"],
         "parameters": {"type": "object", "properties": {}}}]


def _make_instance(tmp_path, bridge_src):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "needle_menu.json").write_text(json.dumps(MENU), encoding="utf-8")
    (d / "bridge.py").write_text(bridge_src, encoding="utf-8")
    return str(d)


def test_bridge_importing_sqlite3_loads_and_answers(tmp_path):
    """A bridge with a module-level `import sqlite3` backs its menu probe.

    In a pip install this exercises the normal path; in the frozen binary
    the same import only works when sqlite3 is force-included in the Nuitka
    build (scripts/build_binary.sh) — the CLI smoke suite covers that end
    to end, this covers the adapter contract.
    """
    d = _make_instance(tmp_path, (
        "import sqlite3\n"
        "\n"
        "def x_count():\n"
        "    con = sqlite3.connect(':memory:')\n"
        "    n = con.execute('SELECT 1').fetchone()[0]\n"
        "    con.close()\n"
        "    return {'n': n}\n"
    ))
    inst = load_menu_instance(d)
    assert inst is not None
    env = inst.router.ask("how many x", use_cache=False)
    assert env["results"][0]["n"] == 1


def test_import_error_is_collected_not_swallowed(tmp_path):
    """A module that fails to import is reported in `errors`, not silent."""
    d = _make_instance(tmp_path, (
        "import sqlite3\n"
        "import module_that_does_not_exist_xyz  # noqa\n"
        "\n"
        "def x_count():\n"
        "    return {'n': 0}\n"
    ))
    errors = {}
    probes = probes_from_menu(d, errors=errors)
    assert probes == []                       # nothing backed the menu
    assert "bridge" in errors
    assert "module_that_does_not_exist_xyz" in errors["bridge"]


def test_load_menu_instance_returns_none_but_reports(tmp_path):
    d = _make_instance(tmp_path, "raise ImportError('DLL load failed')\n")
    errors = {}
    assert load_menu_instance(d, errors=errors) is None
    assert "ImportError" in errors.get("bridge", "")


def test_cli_error_names_the_real_cause(tmp_path):
    """The CLI hint must surface the per-module import failure, not just
    'names must match functions' — the misleading hint cost real debugging
    time on the missing-sqlite3 frozen build."""
    d = _make_instance(tmp_path, "import sqlite3  # ok\nimport nope_xyz\n")
    from neuralosd._cmd._common import _load_menu_instance
    with pytest.raises(SystemExit) as e:
        _load_menu_instance(d)
    msg = str(e.value)
    assert "declares no probe" in msg          # the old message stays
    assert "nope_xyz" in msg                   # + the actual cause
    assert "bridge.py" in msg


def test_bundled_sqlite3_importable():
    """sqlite3 must be importable in whatever runtime runs this test —
    under pytest this is the dev/runtime python; in the frozen binary the
    same import is what the force-include exists for."""
    import sqlite3  # noqa: F401


def test_build_script_force_includes_sqlite3():
    """Guard the build: every Nuitka invocation must force-include sqlite3,
    or a future refactor of build_binary.sh silently re-breaks frozen
    menu-instance bridges on Windows."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "build_binary.sh")
    src = open(script, encoding="utf-8").read()
    assert "--include-package=sqlite3" in src
    assert "--include-module=sqlite3" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
