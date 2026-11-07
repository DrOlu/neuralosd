"""Router.dry_run — the decision half of the gate, published as a verb.

The contract dry_run must keep, forever:

  * it mirrors ask()'s decision path exactly (same normalize, same
    action-intent pre-check, same retrieval, same extraction, same
    coverage, same gate) — proven here against every fixture case
  * it runs NO probe, touches NO cache, writes NO audit line
  * ``would_answer`` is True for both "answered" and "defer_to_model";
    only "refused" means the caller should not bother the instance
"""
import json
import os

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import GATE_VERSION, NoResults, Router

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "refusal_gate.json")


def _fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _arg(spec):
    if spec.get("enum"):
        return {"type": "enum", "values": list(spec["enum"]),
                "required": False}
    if spec.get("pattern"):
        return {"type": "pattern", "pattern": spec["pattern"],
                "required": False}
    if spec.get("type") == "integer":
        return {"type": "integer", "min": spec.get("minimum", 0),
                "max": spec.get("maximum", 10 ** 6), "required": False}
    return {"type": "pattern", "pattern": r".+", "required": False}


def _probes(menu, calls):
    out = []
    for entry in menu:
        name = entry["name"]

        def fn(_name=name, **_kw):
            calls.append(_name)
            return {"probe": _name, "ok": True}

        fn.__name__ = name
        props = ((entry.get("parameters") or {}).get("properties") or {})
        args = {key: _arg(spec) for key, spec in props.items()}
        out.append(probe_dec(description=entry.get("description", ""),
                             triggers=entry.get("triggers", []),
                             args=args or None, name=name)(fn))
    return out


@pytest.fixture()
def env(tmp_path):
    spec = _fixture()
    calls = []
    router = Router(probes=_probes(spec["menu"], calls),
                    cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    return spec, router, calls, tmp_path


# ── conformance: dry_run vs ask, on every fixture case ─────────────────────

def test_gate_version_matches_fixture(env):
    """GATE_VERSION lives beside REFUSAL_REASONS in router.py so the
    vocabulary and its version cannot drift apart."""
    spec, _, _, _ = env
    assert spec["gate_version"] == GATE_VERSION


def test_dry_run_agrees_with_ask_on_every_fixture_case(env):
    spec, router, calls, _ = env
    failures = []
    for case in spec["cases"]:
        q = case["question"]
        dr = router.dry_run(q)
        if case["kind"] == "answer":
            if dr["would"] != "answered":
                failures.append(f"{q}: dry_run said {dr['would']}")
                continue
            try:
                envx = router.ask(q)
            except NoResults as e:
                failures.append(f"{q}: ask refused, dry_run answered")
                continue
            if envx.get("probe") != case["probe"]:
                failures.append(f"{q}: ask probe {envx.get('probe')}")
                continue
            if dr["probe"] != case["probe"]:
                failures.append(f"{q}: dry_run probe {dr['probe']}")
        else:
            if dr["would"] != "refused":
                failures.append(f"{q}: dry_run said {dr['would']}")
                continue
            try:
                router.ask(q)
                failures.append(f"{q}: ask answered, dry_run refused")
                continue
            except NoResults as e:
                got = e.envelope.get("refusal_reason")
            if got != dr["refusal_reason"]:
                failures.append(f"{q}: ask reason {got} != "
                                f"dry_run {dr['refusal_reason']}")
    assert failures == [], "\n".join(failures)


def test_would_answer_covers_everything_but_refusal(env):
    """A defer_to_model is still 'the instance will take this question':
    would_answer must be True for answered AND deferred, False only for
    refused."""
    _, router, _, _ = env
    dr = router.dry_run("how many open incidents")
    assert dr["would"] == "answered" and dr["would_answer"] is True
    dr = router.dry_run("what is the weather in lagos tomorrow")
    assert dr["would"] == "refused" and dr["would_answer"] is False


def test_refusal_reason_is_a_bare_gate_token(env):
    _, router, _, _ = env
    for q in ("delete all returned orders",
              "what is the weather in lagos tomorrow",
              "how many incident attachments exist"):
        dr = router.dry_run(q)
        assert dr["would"] == "refused"
        assert dr["refusal_reason"] in {"action_intent", "dropped_filter",
                                        "no_probe_matches", "low_coverage"}


# ── side-effect freedom ────────────────────────────────────────────────────

def test_dry_run_runs_no_probe_touches_no_cache_or_audit(env):
    _, router, calls, tmp_path = env
    for case in _fixture()["cases"]:
        router.dry_run(case["question"])
    assert calls == [], "dry_run executed a probe"
    assert not (tmp_path / "c.json").exists(), "dry_run wrote the cache"
    assert not (tmp_path / "a.jsonl").exists(), "dry_run wrote the audit"
    # and ask() still works normally afterwards
    assert router.ask("how many open incidents")["probe"] == "open_incidents"


def test_preflight_a_thousand_times_is_free(env):
    _, router, calls, _ = env
    for _ in range(1000):
        router.dry_run("how many open incidents")
    assert calls == []


# ── argument handling mirrors ask() ────────────────────────────────────────

def _probe_int(tmp_path, name="get_page", lo=1, hi=100):
    def fn(page=1, **kw):
        return {"probe": name, "page": page}

    fn.__name__ = name
    return probe_dec(description="One page",
                     triggers=["show page content"],
                     args={"page": {"type": "integer", "min": lo, "max": hi,
                                    "required": True}},
                     name=name)(fn)


def test_out_of_range_args_refuse_the_same_way(tmp_path):
    router = Router(probes=[_probe_int(tmp_path)],
                    cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    dr = router.dry_run("show page 9999 content")
    assert dr["would"] == "refused"
    assert dr["refusal_reason"] == "out_of_range"
    # ask() wraps ArgOutOfRange into the same refusal envelope, so the two
    # doors report the identical refusal_reason to the caller.
    with pytest.raises(NoResults) as e:
        router.ask("show page 9999 content")
    assert e.value.envelope["refusal_reason"] == "out_of_range"


def test_extractable_args_still_answer(tmp_path):
    router = Router(probes=[_probe_int(tmp_path)],
                    cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    dr = router.dry_run("show page 60 content")
    assert dr["would"] == "answered"
    assert dr["probe"] == "get_page"


# ── envelope shape ─────────────────────────────────────────────────────────

def test_dry_run_envelope_shape(env):
    _, router, _, _ = env
    dr = router.dry_run("how many open incidents")
    assert dr["question"] == "how many open incidents"
    assert isinstance(dr["normalized"], str) and dr["normalized"]
    assert dr["menu_version"] == router.menu_version
    assert dr["gate_version"] == GATE_VERSION
    assert isinstance(dr["ms"], float) and dr["ms"] >= 0.0
    assert isinstance(dr["scores"], list) and len(dr["scores"]) <= 3
    assert isinstance(dr["candidates"], list) and len(dr["candidates"]) <= 3
    assert dr["probe"] == "open_incidents"
    assert dr["refusal_reason"] is None


def test_scores_ranked_desc_and_capped_at_three(tmp_path):
    probes = []
    for name in ("alpha_report", "alpha_summary", "alpha_detail",
                 "alpha_export"):
        def fn(**kw):
            return {"ok": True}
        fn.__name__ = name
        probes.append(probe_dec(description=f"{name} thing",
                                triggers=[f"{name.replace('_', ' ')}"],
                                name=name)(fn))
    router = Router(probes=probes, cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    dr = router.dry_run("alpha report summary detail export")
    # scores = the raw ranking (max 3, best first); candidates = the probes
    # that survived the execution gauntlet (also capped, also best first).
    # They are DIFFERENT lists by design and must not be conflated.
    for key in ("scores", "candidates"):
        assert len(dr[key]) <= 3
        vals = [e["score"] for e in dr[key]]
        assert vals == sorted(vals, reverse=True)
        assert all("probe" in e for e in dr[key])
    assert all(e["probe"].startswith("alpha_") for e in dr["scores"])
    assert len({e["probe"] for e in dr["scores"]}) == len(dr["scores"])
