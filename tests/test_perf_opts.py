"""Non-breaking performance work: parallel observe, mapper verdict cache,
boxlite REST-preferred door. Every test is hermetic - no network, no boxlite,
no microVM."""
import json
import os
import time

import pytest

import base64 as _b64mod

import neuralosd.reasoning as R
from neuralosd.reasoning import observations_from


def _mk_probe(name, payload, delay=0.0):
    def fn(**kw):
        if delay:
            time.sleep(delay)
        return payload
    fn.__name__ = name
    fn._probe = type("M", (), {"name": name, "description": "d",
                               "triggers": [name], "args": {},
                               "pii": [], "conf_gate": None,
                               "tier": "x", "confirm": False,
                               "min_coverage": None,
                               "_vocab_tokens": lambda s: frozenset()})()
    return fn


# ── parallel observe ───────────────────────────────────────────────────────

def test_parallel_observe_returns_identical_results_to_serial(tmp_path):
    probes = [_mk_probe(f"p{i}", {"value": i, "rows": [{"x": i}]})
              for i in range(12)]
    serial = observations_from(probes, max_workers=1)
    par = observations_from(probes, max_workers=8)
    assert list(serial.keys()) == list(par.keys())       # same order
    assert serial == par                                  # same content


def test_parallel_observe_drops_failures_like_serial(tmp_path):
    probes = [_mk_probe("ok1", {"n": 1}),
              _mk_probe("ok2", {"n": 2})]
    def boom(**kw):
        raise RuntimeError("nope")
    boom.__name__ = "boom"
    boom._probe = type("M", (), {"name": "boom", "description": "d",
                                 "triggers": ["boom"], "args": {},
                                 "pii": [], "conf_gate": None,
                                 "tier": "x", "confirm": False,
                                 "min_coverage": None,
                                 "_vocab_tokens": lambda s: frozenset()})()
    probes.insert(1, boom)
    got = observations_from(probes, max_workers=4)
    assert set(got) == {"ok1", "ok2"}


def test_workers_env_one_forces_serial(monkeypatch):
    monkeypatch.setenv("NEURALOSD_OBSERVE_WORKERS", "1")
    probes = [_mk_probe(f"p{i}", {"v": i}) for i in range(6)]
    got = observations_from(probes)
    assert set(got) == {f"p{i}" for i in range(6)}


def test_parallel_observe_is_actually_faster_with_slow_probes():
    """8 probes x 0.5s each: serial ~4s, parallel(8) ~0.5-1s."""
    probes = [_mk_probe(f"p{i}", {"n": i}, ) for i in range(8)]
    def slow_maker(i):
        def fn(**kw):
            time.sleep(0.5)
            return {"n": i}
        fn.__name__ = f"p{i}"
        fn._probe = probes[i]._probe
        return fn
    probes = [slow_maker(i) for i in range(8)]
    t0 = time.time(); observations_from(probes, max_workers=1); t_s = time.time()-t0
    t0 = time.time(); observations_from(probes, max_workers=8); t_p = time.time()-t0
    assert t_p < t_s * 0.6, f"parallel ({t_p:.2f}s) not faster than serial ({t_s:.2f}s)"


# ── mapper verdict cache ───────────────────────────────────────────────────

class _StubMapper(R.__class__ if False else object):
    pass


def _mapper_with_counter(monkeypatch, payload, http_calls):
    from neuralosd.reasoning import OllamaMapper, _VERDICT_CACHE
    _VERDICT_CACHE.clear()

    class M(OllamaMapper):
        def _post(self, payload_dict):
            http_calls.append(payload_dict)
            return {"message": {"content": json.dumps(payload)}}
    m = M(model="stub", url="http://stub")
    m.cache = True
    return m


def test_repeat_map_ids_hits_the_cache_not_the_network(monkeypatch):
    from neuralosd.derived import Quantity
    inv = [Quantity("a.b", "a", "b", "scalar", 1)]
    http_calls = []
    m = _mapper_with_counter(monkeypatch,
                             {"numerator": "a.b", "denominator": "c.d"},
                             http_calls)
    r1 = m.map_ids("q", "s", inv)
    r2 = m.map_ids("q", "s", inv)
    assert len(http_calls) == 1, "the second identical call must be cached"
    assert r1 == r2


def test_no_cache_env_disables_the_verdict_cache(monkeypatch):
    from neuralosd.derived import Quantity
    from neuralosd.reasoning import OllamaMapper, _VERDICT_CACHE
    _VERDICT_CACHE.clear()
    monkeypatch.setenv("NEURALOSD_REASON_NO_CACHE", "1")
    calls = []
    from neuralosd.reasoning import OllamaMapper as OM
    class M(OM):
        def _post(self, payload_dict):
            calls.append(1)
            return {"message": {"content": json.dumps(
                {"numerator": "a.b", "denominator": "c.d"})}}
    m = M(model="stub", url="http://stub")
    m.cache = False
    inv = [Quantity("a.b", "a", "b", "scalar", 1)]
    m.map_ids("q", "s", inv)
    m.map_ids("q", "s", inv)
    assert len(calls) == 2


def test_a_different_question_misses_the_cache(monkeypatch):
    from neuralosd.derived import Quantity
    inv = [Quantity("a.b", "a", "b", "scalar", 1)]
    calls = []
    m = _mapper_with_counter(monkeypatch,
                             {"numerator": "a.b", "denominator": "c.d"},
                             calls)
    m.map_ids("question one", "s", inv)
    m.map_ids("question two", "s", inv)
    assert len(calls) == 2


# ── boxlite REST-preferred door ────────────────────────────────────────────

def test_rest_url_returns_configured_endpoint(tmp_path, monkeypatch):
    """The REST decision logic: configured + reachable = URL, otherwise None."""
    from neuralosd._cmd import door
    monkeypatch.setenv("NEURALOSD_BOXLITE_URL", "http://127.0.0.1:8100")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: type("R", (), {
                            "status": 200,
                            "__enter__": lambda s: s,
                            "__exit__": lambda s, *a: False,
                            "read": lambda s: b"[]"})())
    assert door._rest_url() == "http://127.0.0.1:8100"


def test_rest_url_returns_none_when_not_configured(monkeypatch):
    from neuralosd._cmd import door
    monkeypatch.delenv("NEURALOSD_BOXLITE_URL", raising=False)
    assert door._rest_url() is None


def test_rest_url_returns_none_when_unreachable(monkeypatch):
    import urllib.request
    from neuralosd._cmd import door
    monkeypatch.setenv("NEURALOSD_BOXLITE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            OSError("refused")))
    assert door._rest_url() is None


def test_rest_unreachable_falls_back_to_child(monkeypatch, tmp_path):
    import sys as _sys
    from neuralosd._cmd import door
    monkeypatch.setenv("NEURALOSD_BOXLITE_URL", "http://127.0.0.1:1")
    spawned = []
    import base64 as _b64
    class FakeChild:
        returncode = 0
        def __init__(self, *a, **k): pass
        @property
        def stdout(s): return _b64.b64encode(b"data").decode().encode()
        def communicate(self, timeout=None): return self.stdout, b""
        def terminate(self): pass
    monkeypatch.setattr(door.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or FakeChild())
    monkeypatch.setattr(door.sys, "executable", _sys.executable)
    out = door._forward_request("bx", 18921, b"GET / HTTP/1.1\r\n\r\n",
                                box_dir="/app/bx")
    assert out == b"data"
    assert spawned


def test_no_boxlite_url_uses_the_child_path(monkeypatch):
    from neuralosd._cmd import door
    monkeypatch.delenv("NEURALOSD_BOXLITE_URL", raising=False)
    assert door._rest_url() is None
