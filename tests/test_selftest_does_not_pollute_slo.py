"""The refusal SLO must measure USERS, not the self-test harness.

End-to-end: seed a golden bank, let a human ask real questions, replay the
bank — then `refusal_rate()` / `fleet_report()` must count ONLY the human
asks. On the broken tree this fails two ways at once: bank replays inflate
the denominator AND the no-model-fallback refusal rows (no `ts`) vanish from
both sides of the ratio.

Also pins the new `sources=` parameter WITHOUT changing default semantics:
the default denominator still counts every row (patch-release rule), and
legacy rows with no `source` stamp count as "user".
"""
import json
import os
import time

import pytest

from neuralosd import probe as probe_dec
from neuralosd.instance import Instance
from neuralosd.loop import fleet_report, refusal_rate


def _probe(name, triggers, fn):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     args={}, name=name)(fn)


def _probes():
    def total_sales():
        return [{"total": 42}]

    return [_probe("total_sales", ["total sales"], total_sales)]


BANK = {"items": [
    {"q": "total sales", "expect_probe": "total_sales"},
    {"q": "xyzzy plugh", "expect_refusal": True},
    {"q": "delete all sales", "expect_refusal": True},
    {"q": "wubble floop", "expect_refusal": True},
]}


def _seed_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("NEURALOSD_AUDIT_FILE", raising=False)
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    # two human asks: one answered, one refused
    inst.ask("total sales")
    with pytest.raises(Exception):
        inst.ask("xyzzy plugh")   # a refusal is the expected outcome
    return inst


def test_golden_traffic_stays_out_of_the_slo(tmp_path, monkeypatch):
    inst = _seed_instance(tmp_path, monkeypatch)
    inst.golden_run(BANK)

    stats = refusal_rate(str(tmp_path))
    # broken tree: 2 human + 4 bank rows (or worse — refusals vanish entirely)
    assert stats["asks"] == 2, stats
    assert stats["refused"] == 1, stats
    assert stats["refusal_rate"] == 0.5
    assert stats["top_reasons"] and stats["top_reasons"][0][0] == "no_probe_matches"

    rep = fleet_report([str(tmp_path)])
    assert rep["instances"][0]["asks"] == 2
    assert rep["instances"][0]["refused"] == 1


def test_sources_parameter_filters_without_changing_the_default(tmp_path,
                                                                monkeypatch):
    inst = _seed_instance(tmp_path, monkeypatch)
    ledger = os.path.join(str(tmp_path), "ask_audit.jsonl")
    # simulate what the OLD bug produced: self-test rows physically inside
    # the user ledger (a fleet that already ran goldens before this patch)
    with open(ledger, "a", encoding="utf-8") as fh:
        for src, results in (("golden", [{"total": 42}]), ("trap", None)):
            fh.write(json.dumps({
                "ask_id": f"legacy-{src}", "ts": time.time(),
                "question": f"replay ({src})", "probe": None,
                "menu_version": "legacy", "mode": "refused" if results is None
                else "golden",
                "error": "legacy replay", "results": results,
                "refusal_reason": "no_probe_matches" if results is None
                else None,
                "scores": [], "source": src}) + "\n")

    # default denominator is UNCHANGED: every row in the window counts
    everything = refusal_rate(str(tmp_path))
    assert everything["asks"] == 4
    # the new filter isolates the humans
    humans = refusal_rate(str(tmp_path), sources=("user",))
    assert humans["asks"] == 2
    assert humans["refused"] == 1
    assert humans["refusal_rate"] == 0.5
    # and a non-user source can be inspected on its own
    golden_only = refusal_rate(str(tmp_path), sources=("golden", "trap"))
    assert golden_only["asks"] == 2


def test_legacy_rows_without_source_count_as_user(tmp_path, monkeypatch):
    """A pre-2.9.6 audit has no `source` key at all. The filter must keep
    counting those as user traffic, not drop them on upgrade."""
    monkeypatch.delenv("NEURALOSD_AUDIT_FILE", raising=False)
    ledger = os.path.join(str(tmp_path), "ask_audit.jsonl")
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ask_id": "old-1", "ts": time.time(), "question": "total sales",
            "probe": "total_sales", "menu_version": "old", "mode": "golden",
            "results": [{"total": 42}], "scores": []}) + "\n")
    stats = refusal_rate(str(tmp_path), sources=("user",))
    assert stats["asks"] == 1
    assert stats["refused"] == 0
