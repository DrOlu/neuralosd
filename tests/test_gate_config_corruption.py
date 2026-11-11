"""F9 — a corrupt instance router.json must refuse, not silently re-route.

`_router_config` resolves the instance directory first, then the process
cwd (legacy). An instance file that existed but failed to parse was
skipped by the same `except` that protects the legacy candidates, so the
router quietly used whatever `router.json` happened to sit in the cwd — or
the defaults. The operator's calibrated floor/margin were not in effect and
nothing said so (the E7 divergence, back through corruption).
"""
import json

import pytest

from neuralosd.router import Router


def _router(tmp_path, **kw):
    return Router(probes=[], cache_file=str(tmp_path / ".ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"),
                  config_dir=str(tmp_path), **kw)


def test_corrupt_instance_router_json_refuses_by_name(tmp_path):
    cfg = tmp_path / "router.json"
    cfg.write_text('{"floor": 15', encoding="utf-8")          # torn JSON
    with pytest.raises(SystemExit) as exc:
        _router(tmp_path, name="svc")
    msg = str(exc.value)
    assert str(cfg) in msg
    assert "refusing to fall back" in msg


def test_non_object_instance_router_json_refuses(tmp_path):
    cfg = tmp_path / "svc.router.json"
    cfg.write_text(json.dumps([15, 2]), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _router(tmp_path, name="svc")
    assert "not a JSON object" in str(exc.value)


def test_corrupt_legacy_cwd_router_json_stays_tolerant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "router.json").write_text("nonsense", encoding="utf-8")
    r = Router(probes=[], cache_file=str(tmp_path / ".ask_cache.json"),
               audit_file=str(tmp_path / "ask_audit.jsonl"), name="svc")
    assert r.floor == 3.0                       # cwd legacy, tolerant


def test_instance_file_wins_over_a_corrupt_legacy_file(tmp_path,
                                                       monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "router.json").write_text("nonsense", encoding="utf-8")
    good = tmp_path / "inst"
    good.mkdir()
    (good / "router.json").write_text('{"floor": 11}', encoding="utf-8")
    r = _router(good, name="svc")
    assert r.floor == 11.0
