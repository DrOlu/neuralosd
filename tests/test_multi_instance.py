"""Multi-instance serving — ONE process, MANY instances, routed by path.

The current architecture runs one `neuralosd serve` process per instance.
At 3 instances that's annoying. At 30 it's an operational nightmare.

These tests prove:

  1. PATH ROUTING: /{name}/ask routes to the right instance
  2. ISOLATION: instances don't share cache, audit, or probe results
  3. LAZY LOADING: instances load on first request, not at startup
  4. ERROR ISOLATION: one bad instance doesn't affect others
  5. THE ROUTER: each instance has its own Router with its own menu
"""
import json
import os
import threading

import pytest

from neuralosd.multi import MultiInstanceServer, _PathRouter, make_handler


# ── test instances (built with separated function objects) ─────────────────

def _make_instance_a(tmp_path):
    """A tiny instance: row_count + total_revenue."""
    d = tmp_path / "alpha"
    d.mkdir()
    (d / "probes.py").write_text('''
from neuralosd import probe

@probe(description="How many rows", triggers=["how many rows"], name="row_count")
def row_count():
    return {"count": 42}

@probe(description="Total revenue", triggers=["total revenue"], name="total_revenue")
def total_revenue():
    return {"sum": 100.0}

PROBES = [row_count, total_revenue]
''', encoding="utf-8")
    return str(d)


def _make_instance_b(tmp_path):
    """A different instance: by_genre with a caged enum."""
    d = tmp_path / "beta"
    d.mkdir()
    (d / "probes.py").write_text('''
from neuralosd import probe

@probe(description="By genre", triggers=["by genre", "genre tracks"],
       args={"genre": {"type": "enum", "values": ["Rock", "Jazz"],
                       "required": True, "default": "Rock"}},
       name="by_genre")
def by_genre(genre="Rock"):
    return {"genre": genre, "count": 99}

PROBES = [by_genre]
''', encoding="utf-8")
    return str(d)


# ── the path router ────────────────────────────────────────────────────────

class TestPathRouter:
    def test_extracts_instance_and_action(self):
        name, action = _PathRouter.parse("/bxclinic/ask")
        assert name == "bxclinic" and action == "/ask"

    def test_extracts_instance_only(self):
        name, action = _PathRouter.parse("/bxclinic")
        assert name == "bxclinic" and action == "/"

    def test_root_path_returns_none(self):
        name, _ = _PathRouter.parse("/")
        assert name is None

    def test_deep_path_still_works(self):
        name, action = _PathRouter.parse("/inst/openapi.json")
        assert name == "inst" and action == "/openapi.json"


# ── the multi-instance server ──────────────────────────────────────────────

class TestMultiInstanceServer:
    def _server(self, tmp_path):
        _make_instance_a(tmp_path)
        _make_instance_b(tmp_path / ".." / ".".resolve())
        # simpler: just use tmp_path as the instances dir
        return MultiInstanceServer(instances_dir=str(tmp_path))

    def test_names_discovers_instance_dirs(self, tmp_path):
        _make_instance_a(tmp_path)
        _make_instance_b(tmp_path)
        server = MultiInstanceServer(instances_dir=str(tmp_path))
        assert set(server.names) >= {"alpha", "beta"}

    def test_get_lazy_loads_on_first_request(self, tmp_path):
        _make_instance_a(tmp_path)
        server = MultiInstanceServer(instances_dir=str(tmp_path))
        # no instances loaded yet
        assert len(server._loaded) == 0
        # first get loads it
        inst = server.get("alpha")
        assert inst is not None
        assert inst.name == "alpha"
        # now it's cached
        assert "alpha" in server._loaded

    def test_get_returns_none_for_unknown_instance(self, tmp_path):
        server = MultiInstanceServer(instances_dir=str(tmp_path))
        assert server.get("nonexistent") is None

    def test_instances_are_isolated(self, tmp_path):
        """Each instance has its own Router, cache, and audit — no sharing."""
        _make_instance_a(tmp_path)
        _make_instance_b(tmp_path)
        server = MultiInstanceServer(instances_dir=str(tmp_path))
        a = server.get("alpha")
        b = server.get("beta")
        assert a.router.cache_file != b.router.cache_file
        assert a.router.audit_file != b.router.audit_file
        assert a.name != b.name
        # routing is independent
        assert len(a.router.metas) != len(b.router.metas)

    def test_error_isolation(self, tmp_path):
        """A broken instance doesn't affect other instances."""
        good = tmp_path / "good"
        good.mkdir()
        (good / "probes.py").write_text(
            'from neuralosd import probe\n'
            '@probe(description="OK", triggers=["ok"], name="ok")\n'
            'def ok():\n'
            '    return {"ok": True}\n'
            'PROBES = [ok]\n',
            encoding="utf-8")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "probes.py").write_text(
            "import nonexistent_module_xyz\nPROBES = []\n", encoding="utf-8")

        server = MultiInstanceServer(instances_dir=str(tmp_path))
        good_inst = server.get("good")
        bad_inst = server.get("bad")
        assert good_inst is not None
        assert bad_inst is None
        # good still works
        env = good_inst.ask("ok", use_cache=False)
        assert env["probe"] == "ok"


# ── the HTTP handler ───────────────────────────────────────────────────────

def test_http_handler_routes_by_path(tmp_path):
    """The full HTTP flow: POST /{name}/ask routes to the right instance."""
    from neuralosd.multi import serve_multi
    import threading
    import urllib.request
    import time

    _make_instance_a(tmp_path)
    server = MultiInstanceServer(instances_dir=str(tmp_path))
    port = 18899

    # start the server in a daemon thread
    from neuralosd.multi import make_handler as _mh
    from http.server import ThreadingHTTPServer
    handler = _mh(server)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/alpha/ask",
        data=json.dumps({"question": "how many rows"}).encode(),
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    body = json.loads(resp.read())
    assert body["probe"] == "row_count"
    httpd.shutdown()


# ── the registry (directory scanning) ──────────────────────────────────────

class TestRegistry:
    def test_discover_finds_probes_py_dirs(self, tmp_path):
        from neuralosd.registry import Registry
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "probes.py").write_text("PROBES = []", encoding="utf-8")
        (tmp_path / "beta").mkdir()
        (tmp_path / "beta" / "probes.py").write_text("PROBES = []", encoding="utf-8")
        (tmp_path / "notaninstance").mkdir()
        r = Registry(str(tmp_path))
        found = r.discover()
        assert set(found) == {"alpha", "beta"}

    def test_discover_skips_non_instance_dirs(self, tmp_path):
        from neuralosd.registry import Registry
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "probes.py").write_text("PROBES = []", encoding="utf-8")
        (tmp_path / "empty").mkdir()
        (tmp_path / "readme.txt").write_text("not a dir", encoding="utf-8")
        r = Registry(str(tmp_path))
        assert set(r.discover()) == {"real"}
