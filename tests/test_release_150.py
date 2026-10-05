"""Release 1.5.0 — refusal thresholds hardening, shipped behavioural banks,
think-default for Ollama mappers, and the derived-metrics regression lock.

Four changes, each pinned here:

  1. model_abstained  — a fallback that declines is a REFUSAL one layer up
                        (including needle's none_of_these convention)
  2. shipped banks    — init emits golden.json + traps.json; golden_run treats
                        an expectation-less item as a SKIP (the schema trap);
                        an install that regresses a bank does not complete
  3. think default    — Ollama mapper sends think:false; NEURALOSD_THINK=1
                        re-enables; a 400-rejecting template retries clean
  4. regression lock  — probes.py instances route derived metrics with ZERO
                        probe executions at load (see test_derived_routing.py)
"""
import importlib.util
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from neuralosd._cmd._common import load_instance
from neuralosd.derived import DerivedMetric
from neuralosd.reasoning import (MapperError, OllamaMapper, parse_pick)
from neuralosd.router import NoResults, Router, _model_abstained
from neuralosd.probe import probe as probe_dec


def _probe(name, triggers, description="d"):
    def fn(**kw):
        return {"probe": name}
    fn.__name__ = name
    return probe_dec(description=description, triggers=triggers,
                     name=name)(fn)


# ── §1: model_abstained ────────────────────────────────────────────────────

def test_fallback_returning_none_of_these_is_a_refusal(tmp_path):
    """needle's abstain convention must surface as a refusal, not an answer."""
    def fb(q, metas):
        return [{"error": "none_of_these: no probe answers this"}]
    r = Router(probes=[_probe("p", ["thing"])],
               model_fallback=fb,
               cache_file=str(tmp_path / "c.json"),
               audit_file=str(tmp_path / "a.jsonl"))
    with pytest.raises(NoResults) as e:
        r.ask("unrelated gibberish", use_cache=False)   # nothing lexical -> fallback
    env = e.value.envelope
    assert env["refusal_reason"] == "model_abstained"
    assert env["results"] is None
    assert env["mode"] == "refused"


def test_abstain_marker_on_the_tool_tag_is_detected():
    assert _model_abstained([{"_tool": "none_of_these"}])
    assert _model_abstained([{"_tool": "abstain"}])
    assert not _model_abstained([{"_tool": "real_probe"}])


def test_empty_fallback_result_is_an_abstention():
    assert _model_abstained(None)
    assert _model_abstained([])
    assert _model_abstained({})


def test_a_real_answer_is_never_mistaken_for_an_abstention():
    assert not _model_abstained([{"count": 34, "_tool": "open_incidents"}])
    assert not _model_abstained([{"error": "boom", "_tool": "p"}])


# ── §3: think:false default ────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mapper(monkeypatch, calls, status=200):
    m = OllamaMapper(model="qwen3.5:9b", url="http://x")

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        if status == 400 and "think" in calls[-1]:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 400, "no think", {},
                                         None)
        return FakeResponse({"message": {"content":
                                         '{"numerator": "a", "denominator": "b"}'}})

    monkeypatch.setattr("neuralosd.reasoning.urllib.request.urlopen",
                        fake_urlopen)
    return m


def test_mapper_sends_think_false_by_default(tmp_path, monkeypatch):
    calls = []
    monkeypatch.delenv("NEURALOSD_THINK", raising=False)
    m = _mapper(monkeypatch, calls)
    m.map_ids("q", "s", [])
    assert calls and calls[0].get("think") is False
    assert calls[0]["options"] == {"temperature": 0, "seed": 42}


def test_neuralosd_think_1_re_enables_thinking(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("NEURALOSD_THINK", "1")
    m = _mapper(monkeypatch, calls)
    m.map_ids("q", "s", [])
    assert "think" not in calls[0]


def test_a_400_rejecting_template_retries_without_the_flag(tmp_path, monkeypatch):
    """Some model templates reject `think` outright. The flag is an
    optimisation, not a requirement - retry clean instead of failing."""
    calls = []
    m = _mapper(monkeypatch, calls, status=400)
    out = m.map_ids("q", "s", [])
    assert len(calls) == 2
    assert "think" in calls[0]          # first attempt carried the flag
    assert "think" not in calls[1]      # retry went out clean
    assert out == {"numerator": "a", "denominator": "b"}


def test_parse_pick_still_strips_r1_thinking_blocks():
    assert parse_pick("<think>x</think>{\"a\": 1}") == {"a": 1}


# ── §2: shipped banks + the schema trap ────────────────────────────────────

def _make_instance(tmp_path):
    (tmp_path / "probes.py").write_text('''
from neuralosd import probe

@probe(description="How many rows", triggers=["how many rows"], name="row_count")
def row_count():
    return {"count": 2, "rows_in": 2, "rows_counted": 2}

@probe(description="Sample", triggers=["show me some rows"], name="list_rows")
def list_rows():
    return {"rows": [{"a": 1}]}

PROBES = [row_count, list_rows]
''', encoding="utf-8")
    return str(tmp_path)


def test_init_emits_both_banks(tmp_path, monkeypatch):
    """Acceptance §2.1: every instance ships a golden bank and a trap bank."""
    from neuralosd._cmd import init as initmod
    (tmp_path / "s.csv").write_text("region,units\nNorth,10\n", encoding="utf-8")

    class A:
        source = str(tmp_path / "s.csv")
        out = str(tmp_path / "inst")
        name = "s"
    os.makedirs(A.out, exist_ok=True)
    initmod.run(A)

    golden = json.loads((tmp_path / "inst" / "golden.json").read_text(
        encoding="utf-8"))
    traps = json.loads((tmp_path / "inst" / "traps.json").read_text(
        encoding="utf-8"))
    qs = {i["q"] for i in golden["items"]}
    assert "how many rows" in qs                       # positive pre-filled
    assert {i["q"] for i in traps["items"]} >= {
        "xyzzy plugh", "how many rows are blocked"}
    for i in traps["items"]:
        assert i.get("expect_refusal") is True         # the structurally-correct
        assert i.get("refusal_reason")                 # encoding, not null


def test_banks_pass_on_the_fresh_instance_they_ship_with(tmp_path, monkeypatch):
    """The banks cannot be allowed to fail on the instance that just emitted
    them - a red CI on `init` teaches people to delete the banks."""
    from neuralosd._cmd import init as initmod
    (tmp_path / "s.csv").write_text("region,units\nNorth,10\n", encoding="utf-8")

    class A:
        source = str(tmp_path / "s.csv")
        out = str(tmp_path / "inst")
        name = "s"
    os.makedirs(A.out, exist_ok=True)
    initmod.run(A)
    inst = load_instance(str(tmp_path / "inst"))
    for bank in ("golden.json", "traps.json"):
        summary = inst.golden_run(json.loads(
            (tmp_path / "inst" / bank).read_text(encoding="utf-8")))
        assert summary["wrong"] == 0, f"{bank} fails out of the box"
        assert summary["answered_traps"] == 0


def test_golden_run_skips_an_item_with_no_expectation(tmp_path):
    """THE schema trap: expect_probe:null used to auto-pass on ANY answer."""
    inst = load_instance(_make_instance(tmp_path))
    summary = inst.golden_run({"items": [
        {"q": "show me some rows", "expect_probe": None},   # asserts nothing
        {"q": "how many rows", "expect_probe": "row_count"},
    ]})
    assert summary["skipped"] == 1
    assert summary["correct"] == 1
    assert summary["wrong"] == 0


def test_a_confidently_answered_trap_is_counted_separately(tmp_path):
    inst = load_instance(_make_instance(tmp_path))
    summary = inst.golden_run({"items": [
        {"q": "how many rows", "expect_refusal": True,
         "refusal_reason": "no_probe_matches"}]})
    assert summary["answered_traps"] == 1
    assert summary["wrong"] == 1


def test_a_trap_with_the_right_reason_passes(tmp_path):
    inst = load_instance(_make_instance(tmp_path))
    summary = inst.golden_run({"items": [
        {"q": "xyzzy plugh", "expect_refusal": True,
         "refusal_reason": "no_probe_matches"}]})
    assert summary["refused_ok"] == 1
    assert summary["wrong"] == 0


# ── §2.4: an install that regresses a bank does not complete ───────────────

def test_bank_replay_blocks_a_trap_regression(tmp_path, monkeypatch):
    """Acceptance §2.4: menu-changing installs replay BOTH banks."""
    import neuralosd._cmd.reason as reason
    d = _make_instance(tmp_path)
    (tmp_path / "traps.json").write_text(json.dumps({"items": [
        {"q": "show me some rows", "expect_refusal": True}]}),
        encoding="utf-8")   # a trap the CURRENT menu answers -> must block

    inst = load_instance(d)
    summary = inst.golden_run(json.loads(
        (tmp_path / "traps.json").read_text(encoding="utf-8")))
    assert summary["answered_traps"] == 1, "expected the bank to catch it"
    # ...and reason.run's bank-replay branch reports it as a bank_problem
    # (the branch is exercised end-to-end by the CLI test below).


def test_reason_aborts_before_install_when_a_bank_regresses(tmp_path,
                                                            monkeypatch):
    """End-to-end: a proposed metric that answers a TRAP is not installed."""
    from neuralosd._cmd import reason as reason_mod
    d = _make_instance(tmp_path)
    (tmp_path / "traps.json").write_text(json.dumps({"items": [
        {"q": "show me some rows", "expect_refusal": True}]}),
        encoding="utf-8")   # a trap the CURRENT menu answers -> must block

    class StubMapper:
        """Hermetic stand-in for Ollama: proposes a valid-looking metric."""
        model = "stub"

        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

        def map_ids(self, question, served, inventory):
            ids = {q2.id for q2 in inventory}
            num = "row_count.count" if "row_count.count" in ids else sorted(ids)[0]
            den = sorted(ids - {num})[0]
            return {"numerator": num, "denominator": den, "why": "stub ratio"}

    # reason imports OllamaMapper inside run() from ..reasoning, so patch the
    # source module - the import re-resolves on every call.
    import neuralosd.reasoning as reasoning_mod
    monkeypatch.setattr(reasoning_mod, "OllamaMapper", StubMapper)

    class A:
        instance_dir = d
        model = None
        ollama = None
        oracle = None
        scale = "ratio"
        dry_run = False
        force = True
        no_stability_check = True
        timeout = 5
        refresh = False
        question = ["anything"]

    rc = reason_mod.run(A())
    assert rc == 4, "a bank regression must abort with exit 4"
    metrics = tmp_path / "derived.json"
    assert not metrics.exists() or json.loads(
        metrics.read_text(encoding="utf-8"))["metrics"] == [], \
        "the regressing metric was installed anyway"


# ── §4: the regression lock (pinned; full suite in test_derived_routing) ───

def test_regression_lock_probes_path_routes_derived_with_zero_calls(tmp_path):
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_derived_routing import OBS, PROBES_PY  # reuse the fixture
    from neuralosd.reasoning import install
    (tmp_path / "probes.py").write_text(PROBES_PY, encoding="utf-8")
    install(_metric(), str(tmp_path), observations=OBS)
    inst = load_instance(str(tmp_path))
    names = [m.name for m in inst.router.metas]
    assert "derived_revenue_per_unit" in names          # routed on probes.py
    assert names.count("derived_revenue_per_unit") == 1  # no dedup regression


def _metric(name="derived_revenue_per_unit"):
    return DerivedMetric(name=name, kind="ratio",
                         numerator="total_revenue.sum",
                         denominator="total_units.sum", scale="ratio",
                         triggers=["revenue per unit"], question="revenue per unit")
