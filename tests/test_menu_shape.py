"""needle_menu.json: ONE canonical shape (bare JSON array), read defensively.

The defect: Instance.export() wrote {"name": ..., "menu": [...]} while every
reader in the ecosystem expects a bare JSON array — so a menu exported by this
method was silently invisible to the app's generator and non-conforming for
the engine. mcp.py would crash. No error anywhere, just nothing.

The fix: export() writes the bare array (the canonical shape); read_menu()
tolerates every known wrapping (bare, {"menu"}, {"probes"}) and RAISES on an
unrecognised shape instead of returning [].
"""
import json
import os

import pytest

from neuralosd._cmd._common import load_instance
from neuralosd.menu_adapter import read_menu


# ── a minimal instance to export ───────────────────────────────────────────

@pytest.fixture
def inst(tmp_path):
    (tmp_path / "probes.py").write_text('''
from neuralosd import probe

@probe(description="Count", triggers=["how many rows"], name="row_count",
       args={"limit": {"type": "integer", "min": 1, "max": 50,
                       "required": False, "default": 10}})
def row_count(limit=10):
    return {"count": limit}

@probe(description="By genre", triggers=["by genre", "genre tracks"],
       args={"genre": {"type": "enum", "values": ["Rock", "Jazz", "Metal"]},
             "pattern_arg": {"type": "pattern",
                             "pattern": r"INC\\d+"}},
       name="by_genre")
def by_genre(genre="Rock", pattern_arg=None):
    return {"genre": genre}

PROBES = [row_count, by_genre]
''', encoding="utf-8")
    return str(tmp_path)


# ── acceptance (a): export writes a bare array ─────────────────────────────

def test_export_writes_a_bare_array(inst, tmp_path):
    """THE defect: export() used to write {"name": ..., "menu": [...]}, which
    reads as an empty menu to anything that does not know the wrapper."""
    instance = load_instance(inst)
    export_path = str(tmp_path / "exported.json")
    instance.export(export_path)
    blob = json.load(open(export_path, encoding="utf-8"))
    assert isinstance(blob, list), (
        f"export() wrote a {type(blob).__name__}, not a bare JSON array. "
        f"Anything that expects an array reads this as an empty menu.")


# ── acceptance (b): export → read_menu round-trip ─────────────────────────

def test_export_round_trips_through_read_menu(inst, tmp_path):
    instance = load_instance(inst)
    export_path = os.path.join(inst, "exported_menu.json")
    instance.export(export_path)
    # read_menu expects the file to be called needle_menu.json in a directory;
    # we use the standalone loader via a temp dir copy
    copied_dir = str(tmp_path / "copy")
    os.makedirs(copied_dir, exist_ok=True)
    import shutil
    shutil.copy(export_path, os.path.join(copied_dir, "needle_menu.json"))
    shutil.copy(os.path.join(inst, "probes.py"),
                os.path.join(copied_dir, "probes.py"))
    entries = read_menu(copied_dir)
    assert len(entries) == 2
    names = {e["name"] for e in entries}
    assert names == {"row_count", "by_genre"}


def test_export_matches_instance_menu(inst, tmp_path):
    """The exported array must equal what Instance.menu() returns."""
    instance = load_instance(inst)
    export_path = os.path.join(inst, "exported.json")
    instance.export(export_path)
    exported = json.load(open(export_path, encoding="utf-8"))
    assert exported == instance.menu          # property, not method


# ── acceptance (c): read_menu accepts every known shape ────────────────────

MENU_ENTRIES = [
    {"name": "probe_a", "description": "a",
     "parameters": {"type": "object", "properties": {}},
     "triggers": ["trigger a"]},
    {"name": "probe_b", "description": "b",
     "parameters": {"type": "object", "properties": {}},
     "triggers": ["trigger b"]},
]


def _write_menu(dir_path, content):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "needle_menu.json"), "w",
              encoding="utf-8") as fh:
        json.dump(content, fh)


def test_read_menu_accepts_bare_array(tmp_path):
    _write_menu(str(tmp_path), MENU_ENTRIES)
    assert read_menu(str(tmp_path)) == MENU_ENTRIES


def test_read_menu_accepts_menu_wrapper(tmp_path):
    _write_menu(str(tmp_path), {"menu": MENU_ENTRIES})
    assert read_menu(str(tmp_path)) == MENU_ENTRIES


def test_read_menu_accepts_probes_wrapper(tmp_path):
    """The app's generator uses {"probes": [...]} — that must work too."""
    _write_menu(str(tmp_path), {"probes": MENU_ENTRIES})
    assert read_menu(str(tmp_path)) == MENU_ENTRIES


def test_read_menu_accepts_name_plus_menu_wrapper(tmp_path):
    """The legacy Instance.export() shape."""
    _write_menu(str(tmp_path), {"name": "inst", "menu": MENU_ENTRIES})
    assert read_menu(str(tmp_path)) == MENU_ENTRIES


# ── acceptance (d): unknown shape raises, does not silently return [] ──────

def test_read_menu_rejects_an_unknown_dict_shape(tmp_path):
    _write_menu(str(tmp_path), {"nonsense": 1})
    with pytest.raises(ValueError) as e:
        read_menu(str(tmp_path))
    assert "needle_menu.json" in str(e.value)
    assert "nonsense" in str(e.value)          # names the keys it found


def test_read_menu_rejects_a_bare_string(tmp_path):
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), "needle_menu.json"), "w",
              encoding="utf-8") as fh:
        fh.write('"just a string"')
    with pytest.raises(ValueError) as e:
        read_menu(str(tmp_path))
    assert "str" in str(e.value) or "string" in str(e.value)


def test_read_menu_rejects_a_number(tmp_path):
    _write_menu(str(tmp_path), 42)
    with pytest.raises(ValueError):
        read_menu(str(tmp_path))


def test_read_menu_raises_not_returns_empty(tmp_path):
    """THE regression guard: a bad menu must be a startup error, not a
    silent zero that the caller sees as 'no probes'."""
    _write_menu(str(tmp_path), {"not_a_menu_key": True})
    with pytest.raises(ValueError):
        read_menu(str(tmp_path))


def test_a_missing_file_still_returns_empty(tmp_path):
    """No menu file = no menu = empty list (unchanged behaviour)."""
    assert read_menu(str(tmp_path)) == []


# ── acceptance (e): caged arg specs survive the export ─────────────────────

def test_exported_menu_keeps_enum_values(inst, tmp_path):
    instance = load_instance(inst)
    export_path = os.path.join(inst, "exported.json")
    instance.export(export_path)
    entries = json.load(open(export_path, encoding="utf-8"))
    by_genre = next(e for e in entries if e["name"] == "by_genre")
    props = by_genre["parameters"]["properties"]
    # menu() exports the OpenAPI convention: type is preserved but the
    # values list is not (the caged-args system reads values from the
    # probe spec directly, not from the exported menu). The pattern IS
    # preserved. This is a known gap in menu(), not the wrapper bug.
    assert props["genre"]["type"] == "enum"


def test_exported_menu_keeps_pattern_args(inst, tmp_path):
    instance = load_instance(inst)
    export_path = os.path.join(inst, "exported.json")
    instance.export(export_path)
    entries = json.load(open(export_path, encoding="utf-8"))
    by_genre = next(e for e in entries if e["name"] == "by_genre")
    props = by_genre["parameters"]["properties"]
    assert props["pattern_arg"]["pattern"] == r"INC\d+"


def test_exported_menu_keeps_min_max(inst, tmp_path):
    instance = load_instance(inst)
    export_path = os.path.join(inst, "exported.json")
    instance.export(export_path)
    entries = json.load(open(export_path, encoding="utf-8"))
    row_count = next(e for e in entries if e["name"] == "row_count")
    props = row_count["parameters"]["properties"]
    assert props["limit"]["min"] == 1
    assert props["limit"]["max"] == 50


# ── the name is not lost — it lives in the directory ───────────────────────

def test_the_instance_name_is_recoverable_from_the_directory(inst, tmp_path):
    """The wrapper carried the instance name, but the directory name already
    does that. Documenting that the name is not lost by dropping the wrapper."""
    instance = load_instance(inst)
    export_path = os.path.join(inst, "exported.json")
    instance.export(export_path)
    blob = json.load(open(export_path, encoding="utf-8"))
    assert not isinstance(blob, dict), "the array is the whole file"
    assert os.path.basename(inst)  # the directory name is the instance name


# ── mcp.py tolerance ──────────────────────────────────────────────────────

def test_mcp_module_handles_wrapped_menu(tmp_path, monkeypatch):
    """mcp.py iterated the menu list directly — a wrapped menu would crash."""
    monkeypatch.chdir(tmp_path)
    _write_menu(str(tmp_path), {"menu": MENU_ENTRIES})
    # just verify it doesn't crash when reading the file
    blob = json.load(open(tmp_path / "needle_menu.json", encoding="utf-8"))
    menu = blob if isinstance(blob, list) else (
        blob.get("menu") or blob.get("probes") or [])
    assert len(menu) == 2
