"""Tests-first: loop improvements v2.3.0 — the bank/gap/reason loop grows up.

Ten features, each pinned here (lifecycle features — transfer, shadow,
mothball, drift, federation — live in test_loop_lifecycle.py):

  F1  probe birth traps from the reason loop: installing a derived metric
      synthesizes its misuse trap AND proves the refusal before closing.
  F2  refusal clustering in `gaps`: the pattern, not the question — a cluster
      reports how many refused queries one capability-gap cost.
  F3  negative-feedback ingestion: a thumbs-down becomes a bank candidate
      with the user's correction attached, promoted only with evidence.
  F5  oracle provenance: golden items carry WHO verified, WHEN, from WHAT
      source — an evidence chain, not just a number.
  F7  consensus in the reason loop: the stability check can require TWO
      DIFFERENT models to agree, not one model twice.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance

CSV = "region,units\nNorth,100\nSouth,200\nEast,50\nNorth,30\n"


def _build(tmp_path, csv=CSV, name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(csv, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None))
    return out


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res


# ── F1: probe birth traps from the reason loop ─────────────────────────────

def _metric_for(name="derived_units_share", trigger="units share"):
    from neuralosd.derived import DerivedMetric
    return DerivedMetric(
        name=name, kind="ratio",
        numerator="total_units.sum", denominator="row_count.count",
        scale="ratio", description="units per row",
        triggers=[trigger], question="what is the units share",
        provenance={"model": "stub", "verified_against": 100.0})


def test_probe_birth_synthesizes_and_proves_the_trap(tmp_path):
    """Installing a metric synthesizes its misuse trap into traps.json and
    PROVES the refusal with the live router before the loop closes."""
    from neuralosd.derived import load as load_derived, save as save_derived
    from neuralosd.loop import close_probe_birth
    out = _build(tmp_path)
    metric = _metric_for()
    save_derived(os.path.join(out, "derived.json"), [metric])

    summary = close_probe_birth(metric, out)

    traps = json.loads(open(os.path.join(out, "traps.json"),
                            encoding="utf-8").read())
    qs = [t["q"] for t in traps["items"]]
    assert "units share derived_units_share" in qs
    assert summary["refused"] is True          # PROVEN, not assumed
    # ...and the receipt is stored on the metric itself:
    stored = load_derived(os.path.join(out, "derived.json"))[0]
    assert stored.provenance["birth_trap"]["refused"] is True
    assert stored.provenance["birth_trap"]["q"] == \
        "units share derived_units_share"


def test_probe_birth_skips_a_name_that_cannot_carry_a_trap(tmp_path):
    """A metric whose raw name is NOT foreign to its own vocabulary cannot
    have a valid misuse trap (the conforming router would answer it). The
    birth proof skips honestly — no fake trap enters the bank."""
    from neuralosd.derived import load as load_derived, save as save_derived
    from neuralosd.loop import close_probe_birth
    out = _build(tmp_path)
    metric = _metric_for(name="breakdown", trigger="units share")
    save_derived(os.path.join(out, "derived.json"), [metric])

    summary = close_probe_birth(metric, out)

    assert summary["synthesized"] is False
    assert summary["refused"] is None
    traps = json.loads(open(os.path.join(out, "traps.json"),
                            encoding="utf-8").read())
    assert "units share breakdown" not in [t["q"] for t in traps["items"]]


# ── F2: refusal clustering ─────────────────────────────────────────────────

AUDIT = "\n".join(json.dumps(r) for r in [
    {"ts": 1, "question": "shipping cost per unit", "probe": None,
     "results": None, "refusal_reason": "no_probe_matches"},
    {"ts": 2, "question": "what is the shipping cost for each unit",
     "probe": None, "results": None, "refusal_reason": "no_probe_matches"},
    {"ts": 3, "question": "shipping cost divided by units", "probe": None,
     "results": None, "refusal_reason": "below_floor"},
    {"ts": 4, "question": "refund policy for orders", "probe": None,
     "results": None, "refusal_reason": "no_probe_matches"},
    {"ts": 5, "question": "can I refund an order", "probe": None,
     "results": None, "refusal_reason": "no_probe_matches"},
    {"ts": 6, "question": "how many rows", "probe": "row_count",
     "results": [{"count": 4}]},
])


def test_refusal_gaps_cluster_into_patterns(tmp_path):
    """'this gap cost you 3 refused queries, across 3 phrasings' — clusters
    change prioritization from anecdote to signal."""
    from neuralosd._cmd import gaps as gaps_cmd
    log_path = str(tmp_path / "ask_audit.jsonl")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(AUDIT)

    class A:
        log = log_path
        menu = ""
        out = str(tmp_path / "gaps.json")
        export_fingerprints = None
        ingest_fingerprints = None

    gaps_cmd.run(A)
    out = json.loads(open(A.out, encoding="utf-8").read())
    assert out["count"] == 5                       # all refusals mined
    clusters = out["clusters"]
    sizes = sorted((c["size"] for c in clusters), reverse=True)
    assert sizes[0] >= 3                            # the shipping-cost pattern
    top = max(clusters, key=lambda c: c["size"])
    assert top["size"] >= 3
    assert top["common_terms"]                      # "the pattern", visible
    # cost framing, explicitly:
    assert any("refused quer" in c["cost"] for c in clusters)


def test_cluster_is_similarity_based_not_prefix_based(tmp_path):
    from neuralosd.loop import cluster_gaps
    gaps = [{"question": q} for q in
            ["cost of shipping per unit", "unit shipping cost",
             "refund a payment", "payments refund", "weather today"]]
    clusters = cluster_gaps(gaps, threshold=0.34)
    big = [c for c in clusters if c["size"] >= 2]
    # "weather today" must be alone: a singleton gap is NOT forced into a
    # cluster just to make the report look tidy.
    assert all(c["size"] >= 2 for c in big)
    alone = [c for c in clusters if c["size"] == 1]
    assert any("weather" in c["questions"][0] for c in alone)


# ── F3: negative-feedback ingestion ────────────────────────────────────────

def test_thumbs_down_without_correction_becomes_a_golden_candidate(tmp_path):
    """A rejected answer is the highest-signal data there is — but it is NOT
    a trap: the question is legitimate, the answer was wrong. It becomes a
    golden candidate awaiting a verified oracle."""
    from neuralosd.banks import ingest_feedback
    out = _build(tmp_path)
    entry = ingest_feedback(
        instance_dir=out, question="total units by region",
        verdict="bad", correction=None, by="user@corp", ts=1000.0)
    assert entry["kind"] == "golden_candidate"
    cand = json.loads(open(os.path.join(out, "candidates.json"),
                           encoding="utf-8").read())
    item = cand["golden_candidates"][-1]
    assert item["q"] == "total units by region"
    assert item["verified"] is False
    assert item["provenance"]["source"] == "user_feedback"
    assert item["provenance"]["by"] == "user@corp"


def test_thumbs_down_with_correction_attaches_the_correction(tmp_path):
    from neuralosd.banks import ingest_feedback
    out = _build(tmp_path)
    ingest_feedback(instance_dir=out, question="units by region",
                    verdict="bad",
                    correction="breakdown of units by region",
                    by="user@corp", ts=1000.0)
    cand = json.loads(open(os.path.join(out, "candidates.json"),
                           encoding="utf-8").read())
    item = cand["golden_candidates"][-1]
    assert item["q"] == "breakdown of units by region"
    assert item["provenance"]["original_question"] == "units by region"


def test_verdict_invalid_becomes_a_trap_candidate_proven_at_apply(tmp_path):
    """'invalid' means the question itself is out of scope — a trap candidate.
    At apply time the refusal is PROVEN: a trap the router currently answers
    stays a candidate (it would fail the bank otherwise)."""
    from neuralosd.banks import apply_feedback, ingest_feedback
    out = _build(tmp_path)
    ingest_feedback(instance_dir=out, question="delete all rows",
                    verdict="invalid", correction=None, by="secops", ts=1.0)
    # the action-intent gate already refuses "delete all rows", so apply
    # must prove the refusal and promote it into traps.json:
    summary = apply_feedback(out)
    traps = json.loads(open(os.path.join(out, "traps.json"),
                            encoding="utf-8").read())
    assert any(t["q"] == "delete all rows" for t in traps["items"])
    assert summary["promoted_traps"] >= 1
    # and the shipped bank still passes on the instance it ships with:
    inst = load_instance(out)
    s = inst.golden_run(traps)
    assert s["answered_traps"] == 0


# ── F5: oracle provenance ──────────────────────────────────────────────────

def test_golden_records_carry_an_oracle_receipt(tmp_path):
    from neuralosd.banks import record_golden
    out = _build(tmp_path)
    item = record_golden(out, q="how many rows", expect_probe="row_count",
                         value=4, by="olu", source="ledger page 12",
                         method="manual count", ts=1750000000.0)
    assert item["oracle"]["by"] == "olu"
    assert item["oracle"]["value"] == 4
    assert item["oracle"]["source"] == "ledger page 12"
    golden = json.loads(open(os.path.join(out, "golden.json"),
                             encoding="utf-8").read())
    stored = [i for i in golden["items"] if i["q"] == "how many rows"][-1]
    assert stored["oracle"]["method"] == "manual count"
    # ...and the bank still replays clean:
    inst = load_instance(out)
    s = inst.golden_run(golden)
    assert s["wrong"] == 0


def test_receipts_print_the_evidence_chain(tmp_path, capsys):
    from neuralosd.banks import record_golden, receipts
    out = _build(tmp_path)
    record_golden(out, q="how many rows", expect_probe="row_count",
                  value=4, by="olu", source="ledger page 12",
                  method="manual count", ts=1750000000.0)
    text = receipts(out)
    assert "how many rows" in text
    assert "olu" in text and "ledger page 12" in text
    assert "214" not in text                       # no fabricated numbers


# ── F7: consensus across models in the reason loop ─────────────────────────

GOOD = {"numerator": "top_customers.rows[].total_spend",
        "denominator": "overview.grand_total_spend", "why": "top / total"}
OBS = {
    "top_customers": {"returned": 2,
                      "rows": [{"id": 6, "total_spend": 225.60}]},
    "overview": {"grand_total_spend": 451.20},
}
Q = "what share of total spend comes from the top customers"
OTHER = {"numerator": "overview.grand_total_spend",
         "denominator": "top_customers.rows[].total_spend", "why": "flipped"}


class StubMapper:
    def __init__(self, *picks, model="stub"):
        self.picks = list(picks)
        self.model = model

    def map_ids(self, question, served, inventory):
        return self.picks.pop(0) if self.picks else {}

    def available(self):
        return True


def test_consensus_two_models_agree_installs():
    """Two DIFFERENT models picking the same ids is stronger than one model
    agreeing with itself."""
    from neuralosd.reasoning import propose
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD, model="m1"),
                oracle=0.4995, consensus_mapper=StubMapper(GOOD, model="m2"))
    check = {c["name"]: c for c in p.checks}
    assert check["stable_across_models"]["ok"] is True


def test_consensus_models_disagree_no_install():
    from neuralosd.reasoning import propose
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD, model="m1"),
                consensus_mapper=StubMapper(OTHER, model="m2"))
    check = {c["name"]: c for c in p.checks}
    assert check["stable_across_models"]["ok"] is False
    assert not p.ok
    assert any("disagree" in r for r in p.reasons)


def test_no_consensus_mapper_keeps_single_model_stability():
    """Without a second model configured, behaviour is exactly today's —
    one model, twice, honestly labelled."""
    from neuralosd.reasoning import propose
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD, model="m1"))
    names = [c["name"] for c in p.checks]
    assert "stable_across_two_calls" in names
    assert "stable_across_models" not in names
