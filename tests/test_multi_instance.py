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


# ── the CLI layer (run_multi) — the layer the old tests skipped ────────────
# Every test above builds MultiInstanceServer / make_handler directly, which
# is exactly how v2.9.4 shipped a `serve --instances-dir` that crashed with
# NameError: name 'os' is not defined before binding a single socket.
# These tests go through the real CLI entry point.

import argparse
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_cli_instances(tmp_path):
    """Two minimal instance dirs, both answering the 'health' question."""
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "probes.py").write_text(
            'from neuralosd import probe\n'
            '@probe(description="Health check", triggers=["health"], '
            'name="health_probe")\n'
            'def health_probe():\n'
            '    return {"ok": True}\n'
            'PROBES = [health_probe]\n',
            encoding="utf-8")
    return str(tmp_path)


def _serve_ns(instances_dir, port, host="127.0.0.1", token=None):
    return argparse.Namespace(instances_dir=instances_dir, port=port,
                              host=host, strict=False, auth_token=token)


def _wait_healthz(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


class TestServeCli:
    def test_run_multi_binds_and_serves(self, tmp_path):
        """Regression for v2.9.4: run_multi crashed with
        NameError: name 'os' is not defined on line 47 — the CLI path was
        never exercised because tests built the server objects directly."""
        from neuralosd._cmd.serve import run_multi
        d = _make_cli_instances(tmp_path)
        port = _free_port()
        t = threading.Thread(target=run_multi, args=(_serve_ns(d, port),),
                             daemon=True)
        t.start()
        assert _wait_healthz(port), "multi-instance server never came up"

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/instances", timeout=10) as r:
            body = json.loads(r.read())
        assert set(body["instances"]) >= {"alpha", "beta"}

        # ask routes by path, lazy-loads the instance on first hit
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/alpha/ask",
            data=json.dumps({"question": "health"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
        assert body.get("probe") == "health_probe"

        # second instance is independent
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/beta/ask",
            data=json.dumps({"question": "health"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert json.loads(r.read()).get("probe") == "health_probe"

    def test_serve_subprocess_multidir(self, tmp_path):
        """Shell out to `python -m neuralosd serve --instances-dir ...` and
        wait for /healthz. A subprocess is the only way to catch a crash that
        happens before the server binds — in-process threads mask it."""
        d = _make_cli_instances(tmp_path)
        port = _free_port()
        # warm the interpreter/module cache first: a cold CI runner (macOS
        # especially) can spend its whole poll window on the very first
        # import of the CLI chain, which would read as "crash before bind".
        # --help exercises dispatch machinery without binding anything.
        warm = subprocess.run(
            [sys.executable, "-m", "neuralosd", "--help"],
            capture_output=True, timeout=120)
        assert warm.returncode == 0, (
            "python -m neuralosd --help failed; stderr=\n"
            + warm.stderr.decode(errors="replace"))
        proc = subprocess.Popen(
            [sys.executable, "-m", "neuralosd", "serve",
             "--instances-dir", d, "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        up = False
        err = b""
        try:
            up = _wait_healthz(port, timeout=90)
            if up:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/instances", timeout=10) as r:
                    assert set(json.loads(r.read())["instances"]) == {"alpha", "beta"}
        finally:
            proc.terminate()
            try:
                _, err = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, err = proc.communicate()
        err = (err or b"").decode(errors="replace")
        assert up, "subprocess never served /healthz; stderr=\n" + err
        assert "NameError" not in err and "Traceback" not in err, err

    # ── Bug 2 (v2.9.4): --auth-token was silently ignored in multi mode ────

    def test_run_multi_enforces_auth_token(self, tmp_path):
        """With a token, every route except the liveness/readiness probes
        returns 401 without credentials — same contract as single-instance
        serve. On v2.9.4 the token was accepted on the CLI and then dropped:
        serve_multi() had no auth parameter and no 401 anywhere."""
        from neuralosd._cmd.serve import run_multi
        d = _make_cli_instances(tmp_path)
        port = _free_port()
        token = "s3cret-token-xyz"
        t = threading.Thread(
            target=run_multi, args=(_serve_ns(d, port, token=token),),
            daemon=True)
        t.start()
        assert _wait_healthz(port), "multi-instance server never came up"
        B = f"http://127.0.0.1:{port}"

        def get(path, headers=None, method="GET", data=None):
            req = urllib.request.Request(B + path, headers=headers or {},
                                         data=data, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        auth = {"Authorization": f"Bearer {token}"}
        # open: liveness + readiness, with and without credentials
        assert get("/healthz")[0] == 200
        assert get("/alpha/healthz")[0] == 200
        assert get("/healthz", auth)[0] == 200
        code, _ = get("/alpha/ready")          # readiness stays OPEN ...
        assert code != 401, "/ready must not require auth"
        # locked: instance data routes
        assert get("/instances")[0] == 401
        assert get("/alpha/openapi.json")[0] == 401
        assert get("/alpha/menu")[0] == 401
        assert get("/alpha/ask", method="POST",
                   data=json.dumps({"question": "health"}).encode())[0] == 401
        # wrong token is still a 401
        assert get("/instances",
                   {"Authorization": "Bearer wrong-token"})[0] == 401
        # correct credentials unlock everything
        assert get("/instances", auth)[0] == 200
        code, body = get("/alpha/ask", auth, method="POST",
                         data=json.dumps({"question": "health"}).encode())
        assert code == 200 and body.get("probe") == "health_probe"
        # X-API-Token is honoured too (single-instance contract)
        assert get("/instances", {"X-API-Token": token})[0] == 200

    def test_run_multi_token_from_env_and_threading(self, tmp_path,
                                                    monkeypatch, capsys):
        """$NEURALOSD_API_TOKEN is honoured in multi mode, and the token is
        actually threaded into serve_multi (v2.9.4 dropped it on the floor)."""
        from neuralosd import multi
        d = _make_cli_instances(tmp_path)
        seen = {}

        def fake_serve(server, host="127.0.0.1", port=0, auth_token=None):
            seen["auth"], seen["host"] = auth_token, host

        monkeypatch.setattr(multi, "serve_multi", fake_serve)
        monkeypatch.setenv("NEURALOSD_API_TOKEN", "env-token-42")
        from neuralosd._cmd.serve import run_multi
        run_multi(_serve_ns(d, 8877, host="0.0.0.0"))
        assert seen["auth"] == "env-token-42"
        assert seen["host"] == "0.0.0.0"

    def test_run_multi_refuses_wide_bind_without_token(
            self, tmp_path, monkeypatch):
        """No token + non-loopback bind fails closed (3.0.2 policy). Wide
        binding without a credential is not a warning."""
        from neuralosd import multi
        d = _make_cli_instances(tmp_path)
        monkeypatch.delenv("NEURALOSD_API_TOKEN", raising=False)
        called = []
        monkeypatch.setattr(multi, "serve_multi",
                            lambda *a, **k: called.append(1))
        from neuralosd._cmd.serve import run_multi
        with pytest.raises(SystemExit) as exc:
            run_multi(_serve_ns(d, 8877, host="0.0.0.0"))
        assert "refusing to bind" in str(exc.value)
        assert not called


# ── F7: the multi door enforces the same request contract as serve ─────────
# It used to coerce: {"question": 42} became "42" and "" became a real ask.

@pytest.fixture()
def multi_live(tmp_path):
    from http.server import ThreadingHTTPServer
    _make_instance_a(tmp_path)
    server = MultiInstanceServer(instances_dir=str(tmp_path))
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                make_handler(server))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _multi_post(url, body):
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


@pytest.mark.parametrize("body,fragment", [
    (b'{"question": 42}', "question must be a string"),
    (b'{"question": ""}', "empty question"),
    (b'{"question": "   "}', "empty question"),
    (b'[]', "must be a JSON"),
    (b'{not json', "malformed JSON"),
])
def test_multi_ask_rejects_malformed_bodies_like_serve(multi_live, body,
                                                       fragment):
    code, parsed = _multi_post(f"{multi_live}/alpha/ask", body)
    assert code == 400, parsed
    assert fragment in parsed["error"], parsed


def test_multi_routes_ignore_the_query_string(multi_live):
    """F7: /healthz?lb=1 and /alpha/ask?trace=1 are the same routes; the
    handler normalized for AUTH but then dispatched on the raw path."""
    with urllib.request.urlopen(f"{multi_live}/healthz?lb=1", timeout=5) as r:
        assert r.status == 200
    code, body = _multi_post(
        f"{multi_live}/alpha/ask?trace=1",
        json.dumps({"question": "how many rows"}).encode())
    assert code == 200, body
    assert body["probe"] == "row_count"
