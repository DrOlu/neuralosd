"""Tests for the on-device model fallback path.

Two layers:

* the **router** decides *when* to consult the model and how to attribute the
  result — tested with a fake fallback, so no model is loaded;
* the **bridge** turns probes into needle tools and parses the engine
  envelope — tested with a fake ``needle`` module, so no engine is loaded.

Both are hermetic and fast; the real engine is exercised by the live CLI.
"""
import json
import sys
import types

import pytest

from neuralosd import probe as probe_dec
from neuralosd.instance import Instance
from neuralosd.router import NoResults


# ── helpers ────────────────────────────────────────────────────────────────

def _inst(tmp_path, fallback=None, probes=None):
    def revenue():
        return {"revenue": 100}

    def rows():
        return {"count": 5}

    ps = probes or [
        probe_dec(description="total revenue", triggers=["total revenue",
                                                         "revenue"],
                  name="total_revenue")(revenue),
        probe_dec(description="count rows", triggers=["how many rows", "count"],
                  name="row_count")(rows),
    ]
    return Instance(name="t", probes=ps, model_fallback=fallback,
                    state_dir=str(tmp_path))


# ── router: when is the model consulted? ───────────────────────────────────

def test_deterministic_path_never_calls_the_model(tmp_path):
    """FALSE POSITIVE guard: a routable question must not pay for the model."""
    calls = []

    def fake_fallback(q, metas):
        calls.append(q)
        return [{"from": "model"}]

    inst = _inst(tmp_path, fake_fallback)
    env = inst.ask("total revenue")
    assert env["mode"] == "deterministic"
    assert env["results"][0]["_tool"] == "total_revenue"
    assert calls == []


def test_model_used_when_retrieval_is_empty(tmp_path):
    """FALSE NEGATIVE guard: a zero-overlap paraphrase must reach the model."""
    seen = {}

    def fake_fallback(q, metas):
        seen["question"] = q
        seen["metas"] = [m.name for m in metas]
        return [{"answer": 42, "_tool": "row_count"}]

    inst = _inst(tmp_path, fake_fallback)
    env = inst.ask("give me a quick briefing")      # shares no token
    assert env["mode"] == "model"
    assert env["results"][0]["answer"] == 42
    assert seen["question"] == "give me a quick briefing"
    # the WHOLE menu is offered when retrieval found nothing
    assert set(seen["metas"]) == {"total_revenue", "row_count"}


def test_model_gets_the_retrieved_subset_when_args_are_missing(tmp_path):
    def only_caged(label):
        return {"label": label}

    caged = probe_dec(
        description="revenue by period",
        triggers=["total revenue", "revenue"],
        args={"period": {"type": "enum", "values": ["monthly", "annual"]}},
        name="revenue_by_period")(only_caged)

    seen = {}

    def fake_fallback(q, metas):
        seen["metas"] = [m.name for m in metas]
        return [{"label": "monthly", "_tool": "revenue_by_period"}]

    inst = _inst(tmp_path, fake_fallback, probes=[caged])
    env = inst.ask("total revenue")     # matches, but no period in the text
    assert env["mode"] == "model"
    assert seen["metas"] == ["revenue_by_period"]


def test_used_probe_comes_from_the_tool_tag(tmp_path):
    """The model may pick a different probe than rank-1; attribute honestly."""

    def a():
        return {"a": 1}

    def b():
        return {"b": 2}

    pa = probe_dec(description="alpha", triggers=["alpha"], name="alpha")(a)
    pb = probe_dec(description="beta", triggers=["beta"], name="beta")(b)

    def fake_fallback(q, metas):
        return [{"b": 2, "_tool": "beta"}]

    inst = Instance(name="t", probes=[pa, pb], model_fallback=fake_fallback,
                    state_dir=str(tmp_path))
    # a question with NO lexical overlap, so the model is the one deciding
    env = inst.ask("xyzzy plugh")
    assert env["mode"] == "model"
    assert env["probe"] == "beta"       # taken from the model's _tool tag
    assert env["results"][0]["b"] == 2


def test_no_fallback_and_empty_retrieval_refuses_clearly(tmp_path):
    inst = _inst(tmp_path, None)
    with pytest.raises(NoResults) as e:
        inst.ask("give me a quick briefing")
    assert "no probe matched" in str(e.value)


def test_no_fallback_and_unextractable_args_says_so(tmp_path):
    def caged(label):
        return {"label": label}

    p = probe_dec(description="by period", triggers=["revenue by period"],
                  args={"period": {"type": "enum",
                                   "values": ["monthly", "annual"]}},
                  name="by_period")(caged)
    inst = _inst(tmp_path, None, probes=[p])
    with pytest.raises(NoResults) as e:
        inst.ask("revenue by period")
    assert "no model fallback" in str(e.value)


def test_model_returning_nothing_is_an_honest_abstention(tmp_path):
    """A fallback that returns nothing DECLINED. Surfacing that as a refusal
    with model_abstained keeps the no-guessing contract one layer up."""
    inst = _inst(tmp_path, lambda q, metas: None)
    with pytest.raises(NoResults) as e:
        inst.ask("give me a quick briefing")
    env = e.value.envelope
    assert env["refusal_reason"] == "model_abstained"
    assert env["results"] is None
    assert "abstained" in str(e.value)


def test_model_failure_is_not_cached(tmp_path):
    """A transient model/probe failure must not be memoised (see router)."""
    state = {"n": 0}

    def flaky(q, metas):
        state["n"] += 1
        if state["n"] == 1:
            return [{"error": "model hiccup"}]
        return [{"ok": True, "_tool": "row_count"}]

    inst = _inst(tmp_path, flaky)
    first = inst.ask("give me a quick briefing")
    assert "error" in first["results"][0]
    second = inst.ask("give me a quick briefing")
    assert second["results"][0].get("ok") is True
    assert not second.get("cached")


# ── bridge: probe -> tool, envelope -> results ─────────────────────────────

class _FakeEngine:
    def __init__(self, tools):
        self.tools = tools
        self.ran = []

    def run(self, query, max_steps=8, max_new_tokens=512):
        self.ran.append(query)
        # call the FIRST tool with no arguments, like a minimal engine would
        out = self.tools[0]()
        scales = getattr(_FakeEngine, "scales", None)
        return {"type": "respond", "success": True, "error": None,
                "function_calls": [], "results": [out]}

    def close(self):
        pass


@pytest.fixture
def fake_needle(monkeypatch):
    """Install a fake `needle` so the bridge can be tested without the model."""
    mod = types.ModuleType("needle")

    def tool(fn=None, *, triggers=None):
        def deco(f):
            f._triggers = triggers
            return f
        return deco(fn) if fn is not None else deco

    def Needle(tools=None, **kw):
        return _FakeEngine(list(tools or []))

    mod.tool = tool
    mod.Needle = Needle
    monkeypatch.setitem(sys.modules, "needle", mod)

    from neuralosd._cmd import model_bridge
    model_bridge.clear_cache()
    yield mod
    model_bridge.clear_cache()


def _metas():
    def revenue(period: str = "monthly"):
        return {"period": period, "revenue": 100}

    def rows():
        return {"count": 5}

    return [
        probe_dec(description="revenue", triggers=["revenue"],
                  args={"period": {"type": "enum", "values": ["monthly"]}},
                  name="revenue")(revenue)._probe,
        probe_dec(description="rows", triggers=["rows"], name="row_count")(rows)._probe,
    ]


def test_bridge_parses_results_and_tags_the_tool(fake_needle):
    from neuralosd._cmd.model_bridge import model_ask
    metas = _metas()
    got = model_ask("anything", metas)
    assert isinstance(got, list) and got
    assert got[0]["_tool"] == "revenue"
    assert got[0]["revenue"] == 100


def test_bridge_reuses_the_engine_across_questions(fake_needle):
    from neuralosd._cmd import model_bridge
    from neuralosd._cmd.model_bridge import model_ask
    metas = _metas()
    model_ask("one", metas)
    eng1 = next(iter(model_bridge._CACHE.values()))["engine"]
    model_ask("two", metas)
    eng2 = next(iter(model_bridge._CACHE.values()))["engine"]
    assert eng1 is eng2                       # model loaded once
    assert eng1.ran == ["one", "two"]


def test_bridge_cache_key_tracks_the_menu(fake_needle):
    from neuralosd._cmd import model_bridge
    from neuralosd._cmd.model_bridge import model_ask
    metas = _metas()
    model_ask("q", metas)
    assert len(model_bridge._CACHE) == 1
    model_ask("q", metas[:1])                 # a different tool-set
    assert len(model_bridge._CACHE) == 2


def test_bridge_clear_cache(fake_needle):
    from neuralosd._cmd import model_bridge
    from neuralosd._cmd.model_bridge import model_ask
    model_ask("q", _metas())
    assert model_bridge._CACHE
    model_bridge.clear_cache()
    assert not model_bridge._CACHE


def test_bridge_returns_none_without_usable_probes(fake_needle):
    from neuralosd._cmd.model_bridge import model_ask
    assert model_ask("q", []) is None


def test_bridge_surfaces_engine_errors(fake_needle, monkeypatch):
    from neuralosd._cmd.model_bridge import model_ask

    class Boom(_FakeEngine):
        def run(self, *a, **k):
            return {"success": False, "error": "engine exploded",
                    "results": []}

    monkeypatch.setattr(fake_needle, "Needle", lambda tools=None, **kw: Boom(tools or []))
    from neuralosd._cmd import model_bridge
    model_bridge.clear_cache()
    with pytest.raises(RuntimeError, match="engine exploded"):
        model_ask("q", _metas())


def test_bridge_returns_none_on_unsuccessful_run(fake_needle, monkeypatch):
    from neuralosd._cmd.model_bridge import model_ask

    class Unsuccessful(_FakeEngine):
        def run(self, *a, **k):
            return {"success": False, "error": None, "results": []}

    monkeypatch.setattr(fake_needle, "Needle",
                        lambda tools=None, **kw: Unsuccessful(tools or []))
    from neuralosd._cmd import model_bridge
    model_bridge.clear_cache()
    assert model_ask("q", _metas()) is None


def test_bridge_reports_probe_errors_as_data(fake_needle):
    """A probe that raises must reach the model as a result, not a crash."""
    from neuralosd._cmd.model_bridge import model_ask

    def angry():
        raise ValueError("no data")

    m = probe_dec(description="angry", triggers=["angry"], name="angry")(angry)._probe
    got = model_ask("q", [m])
    assert got and "error" in got[0]
    assert "no data" in got[0]["error"]


def test_wrapped_tool_preserves_the_probe_signature(fake_needle):
    from neuralosd._cmd import model_bridge
    metas = _metas()
    called = []
    tool = model_bridge._wrap_probe(metas[0], called)
    import inspect
    assert list(inspect.signature(tool).parameters) == ["period"]
    assert json.loads(tool(period="monthly"))["revenue"] == 100
    assert called == ["revenue"]


def test_bridge_ignores_metas_without_a_function(fake_needle):
    from neuralosd._cmd import model_bridge
    m = _metas()[0]
    m.function = None
    assert model_bridge.model_ask("q", [m]) is None


# ── thread safety (regression: this used to segfault) ──────────────────────

def test_concurrent_model_calls_are_serialised(fake_needle, monkeypatch):
    """REGRESSION: six parallel /ask calls against `serve --model` segfaulted
    the process. Building two needle engines on two threads is unsafe, so ALL
    model work must be serialised — construction included, not just run()."""
    import threading
    import time

    from neuralosd._cmd import model_bridge

    overlap = {"max": 0, "now": 0, "builds": 0}
    guard = threading.Lock()

    def enter():
        with guard:
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])

    def leave():
        with guard:
            overlap["now"] -= 1

    class SlowEngine(_FakeEngine):
        def __init__(self, tools):
            enter()
            try:
                overlap["builds"] += 1
                time.sleep(0.02)          # widen the race window
                super().__init__(tools)
            finally:
                leave()

        def run(self, query, max_steps=8, max_new_tokens=512):
            enter()
            try:
                time.sleep(0.02)
                return super().run(query, max_steps, max_new_tokens)
            finally:
                leave()

    monkeypatch.setattr(fake_needle, "Needle",
                        lambda tools=None, **kw: SlowEngine(tools or []))
    model_bridge.clear_cache()

    metas = _metas()
    errors = []

    def worker():
        try:
            model_ask_safe(metas)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def model_ask_safe(m):
        from neuralosd._cmd.model_bridge import model_ask
        return model_ask("q", m)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert overlap["max"] == 1, f"overlap was {overlap['max']} — not serialised"
    assert overlap["builds"] == 1, "engine should be built once and reused"


def test_engine_is_reused_under_concurrency(fake_needle):
    import threading
    from neuralosd._cmd import model_bridge
    from neuralosd._cmd.model_bridge import model_ask

    model_bridge.clear_cache()
    metas = _metas()

    def worker():
        for _ in range(3):
            model_ask("q", metas)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert len(model_bridge._CACHE) == 1
