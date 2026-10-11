"""Shared refusal-gate conformance suite (cross-product contract).

Runs tests/fixtures/refusal_gate.json — the same fixture ReactorPro and
SuperAgent run — against this router. It exists so the refusal contract
cannot fork again: refusal_reason is a BARE TOKEN, the answered/nothing
exit-code mapping is shared, and a failure here means this product and the
fixture disagree.
"""
import json
import os

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import NoResults, Router

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "refusal_gate.json")


def _fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _arg(spec):
    if spec.get("enum"):
        return {"type": "enum", "values": list(spec["enum"]), "required": False}
    if spec.get("pattern"):
        return {"type": "pattern", "pattern": spec["pattern"], "required": False}
    if spec.get("type") == "integer":
        return {"type": "integer", "min": spec.get("minimum", 0),
                "max": spec.get("maximum", 10 ** 6), "required": False}
    return {"type": "pattern", "pattern": r".+", "required": False}


def _probes(menu):
    out = []
    for entry in menu:
        name = entry["name"]

        def fn(_name=name, **_kw):
            return {"probe": _name, "ok": True}

        fn.__name__ = name
        props = ((entry.get("parameters") or {}).get("properties") or {})
        args = {key: _arg(spec) for key, spec in props.items()}
        out.append(probe_dec(description=entry.get("description", ""),
                             triggers=entry.get("triggers", []),
                             args=args or None, name=name)(fn))
    return out


def _router(tmp_path, menu):
    return Router(probes=_probes(menu), cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"))


def test_fixture_declares_the_shared_contract():
    spec = _fixture()
    assert spec["contract"]["exit_codes"] == {"answered": 0, "refused": 2}
    assert spec["contract"]["reasons_are_bare_tokens"] is True
    for case in spec["cases"]:
        reason = case.get("reason")
        assert reason is None or "(" not in reason


def test_every_conformance_case_matches(tmp_path):
    spec = _fixture()
    router = _router(tmp_path, spec["menu"])
    failures = []
    for case in spec["cases"]:
        question = case["question"]
        if case["kind"] == "answer":
            try:
                env = router.ask(question)
            except NoResults as exc:
                failures.append("%s: refused as %s" % (question, exc.envelope.get("refusal_reason")))
                continue
            if env.get("probe") != case["probe"]:
                failures.append("%s: probe %s != %s" % (question, env.get("probe"), case["probe"]))
        else:
            with pytest.raises(NoResults) as err:
                router.ask(question)
            got = err.value.envelope.get("refusal_reason")
            if got != case["reason"]:
                failures.append("%s: reason %s != %s" % (question, got, case["reason"]))
    assert failures == [], "conformance failures:\n  " + "\n  ".join(failures)


def test_false_refusal_sweep(tmp_path):
    spec = _fixture()
    router = _router(tmp_path, spec["menu"])
    failures = []
    for entry in spec["menu"]:
        question = (entry.get("triggers") or [entry["name"]])[0]
        try:
            router.ask(question)
        except NoResults as exc:
            failures.append("%s trigger %r refused as %s"
                            % (entry["name"], question,
                               exc.envelope.get("refusal_reason")))
    assert failures == [], "false refusals:\n  " + "\n  ".join(failures)
