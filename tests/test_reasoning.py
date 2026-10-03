"""The reasoning loop — deterministic where it can be, constrained where it cannot.

Every test here runs with a STUB mapper. Ollama is never contacted, so this
runs in CI and proves the properties rather than the plumbing:

  * the model never judges whether it is needed
  * the model can only pick from a closed list of observed quantities
  * the model never computes — and its arithmetic, if it offers any, is ignored
  * an unstable or unverifiable mapping is refused, not installed
  * a regression is measured against THIS router's own prior behaviour
"""
import json

import pytest

from neuralosd.derived import DerivedMetric, load
from neuralosd.reasoning import (MapperError, Proposal, backtest, compare_routes,
                                 install, needs_escalation, parse_pick,
                                 propose, snapshot_routes, triggers_from_question,
                                 validate_pick)

# These numbers reproduce the real chinook ratio exactly: the top 10 customers
# spend 451.20 of a 2328.60 grand total, so ORACLE below is the true value and
# a mismatch is a genuine failure rather than an artefact of the fixture.
OBS = {
    "top_customers": {"returned": 2,
                      "rows": [{"id": 6, "total_spend": 225.60},
                               {"id": 26, "total_spend": 225.60}]},
    "overview": {"grand_total_revenue": 2328.60},
}
INV = __import__("neuralosd.derived", fromlist=["x"]).inventory_from_observations(OBS)


class StubMapper:
    """Stands in for a reasoning model. Records what it was asked."""

    def __init__(self, *picks, model="stub"):
        self.picks = list(picks)
        self.model = model
        self.calls = []

    def map_ids(self, question, served, inventory):
        self.calls.append({"question": question, "served": served,
                           "n": len(inventory)})
        return self.picks.pop(0) if self.picks else {}


GOOD = {"numerator": "top_customers.rows[].total_spend",
        "denominator": "overview.grand_total_revenue", "why": "top10 / total"}


# ── 1. the gate is deterministic ───────────────────────────────────────────

def test_a_share_question_the_probe_cannot_answer_escalates():
    got = needs_escalation("what share of revenue is that", "top_customers",
                           probe_text="top customers by spend")
    assert got == "share of"


def test_a_covered_qualifier_does_not_escalate():
    """routing already handles the shape, so do not spend a model on it."""
    assert needs_escalation("what share of revenue", "top_genres",
                            probe_text="genres ranked with revenue share") is None


def test_no_qualifier_does_not_escalate():
    assert needs_escalation("how many customers", "cust", probe_text="cust") is None


def test_an_unknown_probe_escalates_rather_than_answering_silently():
    assert needs_escalation("what share of revenue", None) == "share of"


@pytest.mark.parametrize("q,expect", [
    ("what is the average cost per unit", "per unit"),
    ("revenue per customer", "per customer"),
    ("what proportion of sales", "proportion of"),
])
def test_ratio_qualifiers_are_recognised(q, expect):
    assert needs_escalation(q, "p", probe_text="p") == expect


# ── 2. the model is a closed-list classifier ───────────────────────────────

def test_an_id_outside_the_closed_list_is_rejected():
    clean, why = validate_pick({"numerator": "made_up", "denominator": "x"}, INV)
    assert clean is None
    assert "not in the closed list" in why


def test_both_ids_must_be_in_the_list():
    clean, why = validate_pick(GOOD, INV)
    assert clean["numerator"] == GOOD["numerator"]
    assert why is None


def test_identical_ids_are_rejected():
    clean, why = validate_pick({"numerator": GOOD["numerator"],
                               "denominator": GOOD["numerator"]}, INV)
    assert clean is None and "same quantity" in why


def test_the_string_null_is_a_decline_not_a_failure():
    """deepseek-r1 replies {"numerator": "null"} — a quirk, blessed here."""
    clean, why = validate_pick({"numerator": "null", "denominator": "null"}, INV)
    assert clean is None
    assert "declined" in why


def test_json_null_is_a_decline():
    clean, why = validate_pick({"numerator": None, "denominator": None}, INV)
    assert clean is None and "declined" in why


def test_a_missing_id_is_rejected():
    clean, why = validate_pick({"numerator": GOOD["numerator"]}, INV)
    assert clean is None


def test_garbage_is_rejected_not_guessed():
    assert validate_pick({}, INV)[0] is None
    assert validate_pick(None, INV)[0] is None


def test_parse_pick_strips_r1_thinking_blocks():
    raw = ('<think>the user wants a share... maybe 2328 is right?</think>\n'
           '{"numerator": "a", "denominator": "b"}')
    assert parse_pick(raw) == {"numerator": "a", "denominator": "b"}


def test_parse_pick_handles_no_json():
    assert parse_pick("I cannot answer that.") == {}


def test_the_inventory_offered_to_the_model_excludes_identifiers():
    assert "top_customers.rows[].id" not in {q.id for q in INV}


# ── 3-6. propose(): compute, invariants, verification ──────────────────────

Q = "what share of total revenue comes from the top 10 customers"
ORACLE = 0.193764


def test_propose_computes_here_not_in_the_model():
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD),
                oracle=ORACLE, scale="ratio")
    assert p.ok
    assert p.pick["why"] == "top10 / total"        # the model's words
    assert p.value["numerator"]["value"] == pytest.approx(451.20)
    assert p.value["ratio"] == pytest.approx(ORACLE, abs=1e-6)


def test_a_smuggled_number_in_the_reply_is_ignored():
    smuggled = dict(GOOD, answer=999999.0, percent=99.99, ratio=42.0)
    p = propose(Q, "top_customers", OBS, StubMapper(smuggled, smuggled),
                oracle=ORACLE, scale="ratio")
    assert p.ok
    assert p.value["ratio"] != 42.0
    assert p.value["percent"] != 99.99


def test_the_oracle_is_checked_and_a_mismatch_blocks_install():
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD), oracle=0.99)
    assert not p.ok
    assert any("oracle" in r for r in p.reasons)


def test_a_correct_mapping_matches_the_oracle_exactly():
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD),
                oracle=ORACLE)
    assert p.ok
    assert "matches_outside_oracle" in [c["name"] for c in p.checks if c["ok"]]


def test_no_oracle_is_reported_as_unchecked_not_as_passed():
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD))
    check = next(c for c in p.checks if c["name"] == "matches_outside_oracle")
    assert check["ok"] is None


def test_an_unstable_model_is_refused():
    """It said 'gap' then 'ok' on identical input in an earlier run."""
    other = {"numerator": "top_customers.returned",
             "denominator": "overview.grand_total_revenue"}
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, other),
                oracle=ORACLE)
    assert p.stable is False
    assert not p.ok
    assert any("different answers" in r for r in p.reasons)


def test_stability_can_be_skipped_explicitly():
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD), oracle=ORACLE,
                check_stability=False)
    assert p.stable is None
    assert p.ok


def test_a_share_above_one_is_caught_by_the_deterministic_expectation():
    """numerator >> denominator means the mapping is not part/whole."""
    bad = {"numerator": "overview.grand_total_revenue",
           "denominator": "top_customers.rows[].total_spend"}
    p = propose(Q, "top_customers", OBS, StubMapper(bad, bad))
    assert not p.ok
    assert any("cannot exceed 1" in r for r in p.reasons)


def test_a_declining_model_is_reported_as_a_decline():
    p = propose(Q, "top_customers", OBS, StubMapper({"numerator": "null",
                                                     "denominator": "null"},
                                                    {"numerator": "null",
                                                     "denominator": "null"}))
    assert not p.ok
    assert any("declined" in r for r in p.reasons)


def test_an_unreachable_ollama_is_an_error_not_a_wrong_answer():
    class Dead:
        model = "x"

        def map_ids(self, *a):
            raise MapperError("cannot reach Ollama")
    p = propose(Q, "top_customers", OBS, Dead())
    assert not p.ok
    assert any("Ollama" in r for r in p.reasons)


def test_an_instance_with_no_measurable_quantities_is_refused():
    p = propose(Q, "p", {"p": {"text": "hello"}}, StubMapper(GOOD))
    assert not p.ok


def test_propose_has_no_side_effects(tmp_path):
    propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD), oracle=ORACLE)
    assert not (tmp_path / "derived.json").exists()


def test_the_proposal_carries_its_own_provenance():
    p = propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD), oracle=ORACLE)
    assert p.metric.provenance["model"] == "stub"
    assert p.metric.provenance["verified_against"] == ORACLE
    assert "ids_in_closed_list" in p.metric.provenance["checks"]


# ── triggers: derived from the question, never from the model ──────────────

def test_triggers_come_from_the_question():
    t = triggers_from_question("what share of total revenue comes from top")
    assert t[0] == "what share of total revenue comes from top"
    assert all(isinstance(x, str) for x in t)


def test_a_trigger_is_narrow_not_broad():
    """A model-written trigger list is how a new probe hijacks its siblings."""
    t = triggers_from_question("what is the average shipping cost per unit")
    assert len(t) <= 2
    assert not any(x == "average shipping cost" for x in t[1:])   # no sibling steal


def test_triggers_are_stripped_of_lead_question_words():
    t = triggers_from_question("what is the average shipping cost per unit")
    assert t[1].startswith("average") or "average" in t[1]


def test_an_empty_question_yields_no_triggers():
    assert triggers_from_question("") == []


# ── the backtest must be baseline-free ─────────────────────────────────────

def test_snapshot_records_refusals_as_none():
    def ask(q):
        if q == "good":
            return {"probe": "p"}
        raise RuntimeError("no results")
    assert snapshot_routes(["good", "bad"], ask) == {"good": "p", "bad": None}


def test_compare_detects_a_changed_route():
    before = {"a": "p1", "b": "p2", "c": None}
    after = {"a": "DERIVED", "b": "p2", "c": None}
    diff = compare_routes(before, after)
    assert diff["changed"] == 1
    assert diff["unchanged"] == 2
    assert diff["regressions"][0]["question"] == "a"


def test_the_target_question_is_exempt():
    before, after = {"t": "old", "n": "p"}, {"t": "new", "n": "p"}
    assert compare_routes(before, after, exempt={"t"})["changed"] == 0


def test_a_pre_existing_refusal_is_not_a_regression():
    """The first run of this loop reported 16 'regressions' that all predated
    the change — they were another engine's answers, not this router's."""
    before = {"q": None}
    assert compare_routes(before, {"q": None})["changed"] == 0


def test_an_already_odd_route_is_not_a_regression():
    before = {"q": "weird_but_consistent"}
    assert compare_routes(before, before)["changed"] == 0


def test_backtest_with_explicit_expectations():
    bt = backtest([{"question": "a", "expect": "p"}], lambda q: {"probe": "p"})
    assert bt["passed"] == 1 and bt["failed"] == 0


def test_backtest_counts_a_wrong_route_as_failed():
    bt = backtest([{"question": "a", "expect": "p"}], lambda q: {"probe": "q"})
    assert bt["failed"] == 1


def test_backtest_treats_a_refusal_as_a_miss():
    def ask(q):
        raise RuntimeError("nope")
    assert backtest([{"question": "a", "expect": "p"}], ask)["failed"] == 1


# ── install ────────────────────────────────────────────────────────────────

def _metric(name="m"):
    return DerivedMetric(name=name, kind="ratio", numerator=GOOD["numerator"],
                         denominator=GOOD["denominator"], scale="percent",
                         triggers=["t"])


def test_install_writes_a_readable_spec(tmp_path):
    path = install(_metric(), str(tmp_path))
    assert json.loads(open(path, encoding="utf-8").read())["metrics"][0]["name"] == "m"
    assert [m.name for m in load(path)] == ["m"]


def test_install_replaces_by_name_rather_than_duplicating(tmp_path):
    install(_metric("a"), str(tmp_path))
    install(_metric("b"), str(tmp_path))
    install(_metric("a"), str(tmp_path))
    assert sorted(m.name for m in load(str(tmp_path / "derived.json"))) == ["a", "b"]


def test_install_never_touches_probes_py(tmp_path):
    (tmp_path / "probes.py").write_text("PROBES = []\n", encoding="utf-8")
    install(_metric(), str(tmp_path))
    assert (tmp_path / "probes.py").read_text(encoding="utf-8") == "PROBES = []\n"


def test_the_written_spec_carries_min_coverage(tmp_path):
    install(_metric(), str(tmp_path))
    m = load(str(tmp_path / "derived.json"))[0]
    assert m.min_coverage == 0.6      # so a reload cannot hijack siblings


def test_proposal_json_is_serialisable():
    p = proposal()
    json.dumps(p.to_json())


def proposal():
    return propose(Q, "top_customers", OBS, StubMapper(GOOD, GOOD),
                   oracle=ORACLE, scale="ratio")


# ── the inventory must work for BOTH instance shapes ───────────────────────

def test_observe_handles_a_generated_instance(tmp_path):
    """A probes.py instance has no needle_menu.json — and observed 0 probes."""
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text(
        "from neuralosd import probe\n\n"
        "@probe(description='Count', triggers=['count rows'], name='row_count')\n"
        "def row_count():\n    return {'count': 4, 'rows_in': 4}\n\n"
        "PROBES = [row_count]\n", encoding="utf-8")
    from neuralosd.menu_adapter import observe
    obs = observe(str(d))
    assert "row_count" in obs
    assert obs["row_count"]["count"] == 4


def test_observe_excludes_derived_probes_from_the_inventory(tmp_path):
    """A ratio of a ratio is algebraically fine and analytically suspect, and it
    would let an install feed on its own output."""
    from neuralosd.derived import DerivedMetric, as_probe, is_derived
    p = as_probe(DerivedMetric(name="d", kind="ratio", numerator="a.b",
                               denominator="c.d", triggers=["t"]), OBS)
    assert is_derived(p)
    assert not is_derived(object())


def test_an_empty_directory_observes_nothing(tmp_path):
    from neuralosd.menu_adapter import observe
    assert observe(str(tmp_path)) == {}
