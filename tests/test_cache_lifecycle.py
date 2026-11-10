"""E3 — the ask cache has no transition out of "expired" on v2.9.8.

_cache_load() correctly refuses dead entries (wrong menu_version, ts past
TTL, malformed payload) but _cache_store() only ever appended, so dead rows
accumulated for the life of the instance (measured in the wild: 13 live keys
inside a 2.5 MB .ask_cache.json — and every ask re-serialised the whole
file). These tests pin the new store contract: dead states are evicted on
the next write, the cache is capped at CACHE_MAX with newest kept, a corrupt
file is a miss — never a crash — and nothing "migrates" or fabricates
timestamps: existing files keep loading and are simply pruned on write.
"""
import json
import time

import pytest

import neuralosd.router as router_mod
from neuralosd import Router, probe


def make_router(tmp_path, ttl=3600):
    @probe(description="Count widgets in the database",
           triggers=["count widgets"])
    def count_widgets():
        return {"widgets": 7}

    @probe(description="Total widgets ever produced",
           triggers=["widgets total"])
    def widgets_total():
        return {"total": 42}

    @probe(description="Sum of widget weights",
           triggers=["sum widgets"])
    def sum_widgets():
        return {"sum": 99}

    @probe(description="Widget inventory snapshot",
           triggers=["widget inventory"])
    def widget_inventory():
        return {"in_stock": 13}

    @probe(description="Widgets grouped by warehouse",
           triggers=["widgets summary"])
    def widgets_summary():
        return {"by_wh": {"a": 1, "b": 2}}

    return Router(
        probes=[count_widgets, widgets_total, sum_widgets,
                widget_inventory, widgets_summary],
        cache_file=str(tmp_path / ".ask_cache.json"),
        audit_file=str(tmp_path / "ask_audit.jsonl"),
        ttl=ttl)


def _read_cache(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_cache(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class TestCacheLifecycle:
    def test_absent_then_populated_then_hit(self, tmp_path):
        r = make_router(tmp_path)
        env1 = r.ask("count widgets")
        assert not env1.get("cached")
        env2 = r.ask("count widgets")
        assert env2.get("cached") is True

    def test_expired_entry_is_a_miss(self, tmp_path):
        r = make_router(tmp_path)
        r.ask("count widgets")
        path = tmp_path / ".ask_cache.json"
        c = _read_cache(path)
        key = next(iter(c))
        c[key]["ts"] = time.time() - 7200        # rewind past the TTL
        _write_cache(path, c)
        env = r.ask("count widgets")
        assert not env.get("cached"), "an expired entry must be a miss"

    def test_menu_version_rotation_invalidates(self, tmp_path):
        r = make_router(tmp_path)
        r.ask("count widgets")
        path = tmp_path / ".ask_cache.json"
        c = _read_cache(path)
        key = next(iter(c))
        c[key]["menu_version"] = "rotated-away"
        _write_cache(path, c)
        env = r.ask("count widgets")
        assert not env.get("cached"), \
            "an entry from another menu_version must be a miss"

    def test_dead_entries_evicted_on_store(self, tmp_path):
        r = make_router(tmp_path)
        path = tmp_path / ".ask_cache.json"
        now = time.time()
        _write_cache(path, {
            "expired-entry": {"ts": now - 99999, "menu_version": "v1",
                              "payload": {"results": "stale"}},
            "stale-menu": {"ts": now, "menu_version": "some-other-menu",
                           "payload": {"results": "stale"}},
            "garbage": "not even a dict",
        })
        r.ask("count widgets")                   # one store prunes on write
        c = _read_cache(path)
        assert "garbage" not in c
        assert "expired-entry" not in c
        assert "stale-menu" not in c
        assert len(c) == 1, "only the freshly written live entry remains"
        entry = next(iter(c.values()))
        assert entry["menu_version"] == r.menu_version
        assert entry["payload"]["results"]

    def test_cache_entry_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(router_mod, "CACHE_MAX", 3)
        r = make_router(tmp_path)
        path = tmp_path / ".ask_cache.json"
        questions = ["count widgets", "widgets total", "sum widgets",
                     "widget inventory", "widgets summary"]
        for q in questions:
            r.ask(q)
        c = _read_cache(path)
        assert len(c) <= 3, "the cache must be bounded by CACHE_MAX"
        # newest kept, oldest evicted
        assert "widgets summary" in json.dumps(c)
        assert "count widgets" not in json.dumps(c)

    def test_corrupt_cache_is_a_miss_not_a_crash(self, tmp_path):
        path = tmp_path / ".ask_cache.json"
        path.write_text("{not json", encoding="utf-8")
        r = make_router(tmp_path)
        env = r.ask("count widgets")             # must not raise
        assert env.get("results")
        assert not env.get("cached")
