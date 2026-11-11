"""F13 — the mothballed marker is an atomic, tolerant state file.

`mothballed.json` is instance lifecycle state: it decides which probes are
routable. The old writer truncated the file in place, and the old reader
raised on damage, so a crash could either crash `fleet`/`slo` or (after a
torn write) silently reinstate every demoted probe. The writer now stages
a tmp file and replaces; the reader treats damage as "no demotions", the
same contract `_cmd._common._mothballed_names` already had.
"""
import json

import pytest

from neuralosd.loop import _mothball_path, mothballed_names, reinstate


def _write(inst, blob):
    path = inst / "mothballed.json"
    path.write_text(json.dumps(blob), encoding="utf-8")
    return path


def test_corrupt_marker_reads_as_empty_not_a_crash(tmp_path):
    (tmp_path / "mothballed.json").write_text(
        '{"names": ["row_count"', encoding="utf-8")
    assert mothballed_names(tmp_path) == []
    assert reinstate(tmp_path, "row_count") is False


def test_non_object_or_non_string_entries_are_ignored(tmp_path):
    _write(tmp_path, ["not", "an", "object"])
    assert mothballed_names(tmp_path) == []
    _write(tmp_path, {"names": ["ok", 3, None]})
    assert mothballed_names(tmp_path) == ["ok"]


def test_reinstate_preserves_the_other_names(tmp_path):
    _write(tmp_path, {"names": ["a", "b", "c"], "ts": 1.0})
    assert reinstate(tmp_path, "b") is True
    assert mothballed_names(tmp_path) == ["a", "c"]
    assert json.loads((tmp_path / "mothballed.json").read_text(
        encoding="utf-8"))["ts"] == 1.0


def test_reinstate_is_atomic_when_the_write_fails(tmp_path, monkeypatch):
    """A failing write leaves the OLD file intact instead of truncating it."""
    path = _write(tmp_path, {"names": ["a", "b"], "ts": 1.0})
    before = path.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("neuralosd.loop.json.dump", boom)
    with pytest.raises(RuntimeError):
        reinstate(tmp_path, "a")
    assert path.read_text(encoding="utf-8") == before
    assert mothballed_names(tmp_path) == ["a", "b"]


def test_mothballed_path_is_the_documented_name(tmp_path):
    assert _mothball_path(str(tmp_path)).endswith("mothballed.json")
