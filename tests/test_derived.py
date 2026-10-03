"""Derived metrics — declarative ratios a model may propose but never compute.

The whole safety argument lives here: a spec is DATA, the ids come from a closed
inventory of OBSERVED probe output, and the arithmetic is ours. Nothing a model
emits is executed.
"""
import json
import os

import pytest

from neuralosd.derived import (DerivedError, DerivedMetric, Quantity, as_probe,
                               check_expectation, evaluate, expectation_for,
                               formula_hint, inventory_from_observations, load,
                               resolve, save)
from neuralosd.router import Router, coverage

# What two probes really returned — the inventory is observed, not declared.
OBS = {
    "cust": {"returned": 10, "rows": [{"id": 6, "total_spend": 49.62},
                                      {"id": 26, "total_spend": 47.62}]},
    "over": {"grand_total_revenue": 2328.60, "invoice_lines": 2240},
    "years": {"years": 5, "rows": [{"year": 2009, "revenue": 449.46},
                                   {"year": 2010, "revenue": 481.45}]},
}


# ── the closed inventory ───────────────────────────────────────────────────

def test_inventory_is_observed_not_declared():
    inv = {q.id for q in inventory_from_observations(OBS)}
    assert "over.grand_total_revenue" in inv
    assert "cust.rows[].total_spend" in inv
    assert "cust.returned" in inv


def test_identifiers_are_not_measures():
    """Summing customer ids is arithmetically valid and semantically garbage."""
    inv = {q.id for q in inventory_from_observations(OBS)}
    assert "cust.rows[].id" not in inv


def test_calendar_parts_are_not_measures():
    inv = {q.id for q in inventory_from_observations(OBS)}
    assert "years.rows[].year" not in inv


def test_counts_are_measures():
    inv = {q.id for q in inventory_from_observations(OBS)}
    assert "over.invoice_lines" in inv
    assert "cust.returned" in inv


def test_a_non_dict_observation_is_ignored():
    assert inventory_from_observations({"x": None, "y": "nope"}) == []


def test_booleans_are_not_numbers():
    inv = {q.id for q in inventory_from_observations({"p": {"flag": True}})}
    assert not inv


# ── resolving ──────────────────────────────────────────────────────────────

def test_resolve_scalar():
    q = Quantity("over.grand_total_revenue", "over", "grand_total_revenue",
                 "scalar")
    assert resolve(q, OBS) == 2328.60


def test_resolve_sums_rows():
    q = Quantity("cust.rows[].total_spend", "cust", "rows[].total_spend",
                 "sum_rows")
    assert resolve(q, OBS) == pytest.approx(97.24)


def test_resolve_missing_probe_is_none():
    q = Quantity("nope.x", "nope", "x", "scalar")
    assert resolve(q, OBS) is None


# ── expectations, derived from the QUESTION not the model ──────────────────

def test_share_questions_expect_a_value_at_most_one():
    assert expectation_for("what share of total revenue is that") == "lte_1"
    assert expectation_for("what percentage of sales") == "lte_1"
    assert expectation_for("what proportion of invoices") == "lte_1"


def test_other_questions_are_unconstrained():
    assert expectation_for("what is the average order value") == "any"
    assert expectation_for("how many customers") == "any"


def test_a_share_above_one_is_a_violation():
    """The deterministic check that catches a part/whole mapping error."""
    assert check_expectation(0.19, "lte_1") is None
    assert "cannot exceed 1" in check_expectation(3.5, "lte_1")
    assert check_expectation(3.5, "any") is None


def test_a_zero_share_is_a_violation():
    assert "must be positive" in check_expectation(0.0, "lte_1")


# ── the spec ───────────────────────────────────────────────────────────────

def _metric(**kw):
    base = dict(name="m", kind="ratio",
                numerator="cust.rows[].total_spend",
                denominator="over.grand_total_revenue")
    base.update(kw)
    return DerivedMetric(**base)


def test_valid_metric_passes():
    _metric().validate()


@pytest.mark.parametrize("bad", [
    {"name": "has spaces"}, {"name": "1starts_with_digit"}, {"name": ""},
    {"kind": "code"}, {"scale": "dollars"}, {"numerator": ""},
    {"denominator": ""}, {"min_coverage": 1.5}, {"min_coverage": -0.1},
])
def test_invalid_specs_are_rejected(bad):
    with pytest.raises(DerivedError):
        _metric(**bad).validate()


def test_numerator_and_denominator_must_differ():
    with pytest.raises(DerivedError):
        _metric(numerator="over.grand_total_revenue",
                denominator="over.grand_total_revenue").validate()


def test_roundtrip_through_disk(tmp_path):
    p = str(tmp_path / "derived.json")
    save(p, [_metric(name="a"), _metric(name="b", scale="percent")])
    got = load(p)
    assert [m.name for m in got] == ["a", "b"]
    assert got[1].scale == "percent"


def test_a_missing_file_is_an_empty_registry(tmp_path):
    assert load(str(tmp_path / "nope.json")) == []


def test_formula_hint():
    assert "total_spend" in formula_hint(_metric())


# ── evaluating ─────────────────────────────────────────────────────────────

def test_evaluate_computes_the_ratio():
    v = evaluate(_metric(), OBS)
    assert v["numerator"]["value"] == pytest.approx(97.24)
    assert v["denominator"]["value"] == pytest.approx(2328.60)
    assert v["ratio"] == pytest.approx(97.24 / 2328.60, abs=1e-6)
    assert v["unit"] == "ratio"
    assert v["_derived"] is True


def test_evaluate_percent_scale():
    v = evaluate(_metric(scale="percent"), OBS)
    assert v["unit"] == "%"
    assert v["value"] == v["percent"]


def test_evaluate_rejects_an_unknown_id():
    with pytest.raises(DerivedError):
        evaluate(_metric(numerator="does.not.exist"), OBS)


def test_evaluate_rejects_a_zero_denominator():
    obs = dict(OBS, zero={"n": 0.0})
    with pytest.raises(DerivedError):
        evaluate(_metric(denominator="zero.n"), obs)


def test_evaluate_never_returns_a_number_from_a_model():
    """There is no code path in which a caller-supplied value is trusted."""
    v = evaluate(_metric(), OBS)
    assert round(v["numerator"]["value"] +
                 v["denominator"]["value"], 2) == pytest.approx(2425.84)


# ── as a routable probe ────────────────────────────────────────────────────

def test_as_probe_produces_a_routable_probe():
    p = as_probe(_metric(name="share_top", triggers=["what share of revenue"]),
                 OBS)
    meta = p._probe
    assert meta.name == "share_top"
    assert meta.min_coverage == 0.6      # opt-in, so it cannot hijack


def test_a_derived_probe_evaluates_through_the_router(tmp_path):
    p = as_probe(_metric(name="share_top",
                         triggers=["what share of the grand total revenue"]),
                 OBS)
    r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
               audit_file=str(tmp_path / "a.jsonl"))
    env = r.ask("what share of the grand total revenue")
    assert env["results"][0]["ratio"] == pytest.approx(97.24 / 2328.60, abs=1e-6)


# ── coverage: the anti-hijack mechanism ────────────────────────────────────

def test_coverage_distinguishes_touching_from_containing():
    """2-of-2 and 2-of-8 both score 2; coverage tells them apart.

    STOP words are removed first, so "share of total revenue comes from top"
    is five tokens (share, total, revenue, comes, top) — 'of' and 'from' are
    function words.
    """
    p = as_probe(_metric(name="share_top",
                         triggers=["share of total revenue comes from top"]),
                 OBS)
    meta = p._probe
    from neuralosd.router import tokens
    assert coverage(meta, tokens("total revenue")) == pytest.approx(0.4)
    assert coverage(meta, tokens("share of total revenue comes from top")) == 1.0


def test_a_low_coverage_question_does_not_reach_the_derived_probe(tmp_path):
    """The real hijack: a 2-token question captured an 8-token trigger."""
    p = as_probe(_metric(name="share_top",
                         triggers=["share of total revenue comes from top"]),
                 OBS)
    r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
               audit_file=str(tmp_path / "a.jsonl"))
    from neuralosd.router import NoResults
    with pytest.raises(NoResults):
        r.ask("total revenue")


def test_probes_without_min_coverage_are_unaffected():
    from neuralosd.probe import probe as probe_deco

    @probe_deco(description="d", triggers=["total revenue"], name="t")
    def t():
        return {"n": 1}
    assert t._probe.min_coverage is None
    from neuralosd.router import tokens
    # 1 of the trigger's 2 tokens — the original scoring's blind spot, which
    # only probes that OPT IN to min_coverage are protected from.
    assert coverage(t._probe, tokens("total")) == pytest.approx(0.5)


# ── truncated row lists: the trap that an oracle caught, deterministically ──
#
# chinook_albums returns 15 rows out of 347 albums. A model asked for
# 'tracks per album' summed those 15 and divided by 347 -> 0.53 where the truth
# is 10.09. Every id it used was real, the pick was stable, and the wording was
# plausible. Only the observation itself shows the sum covers 15 of 347.

TRUNCATED = {
    "albums": {"total_in_source": 347, "returned": 15,
               "rows": [{"id": 1, "tracks": 9}, {"id": 2, "tracks": 12}]},
    "over": {"grand_total_revenue": 2328.60, "tracks_sold": 2240},
}


def test_summing_a_truncated_row_list_is_refused():
    q = Quantity("albums.rows[].tracks", "albums", "rows[].tracks", "sum_rows")
    with pytest.raises(DerivedError) as e:
        resolve(q, TRUNCATED)
    assert "TRUNCATED" in str(e.value)
    assert "2 rows" in str(e.value) and "total_in_source=347" in str(e.value)


def test_a_complete_row_list_still_sums():
    complete = {"albums": {"total_in_source": 2, "returned": 2,
                           "rows": [{"id": 1, "tracks": 9},
                                    {"id": 2, "tracks": 12}]}}
    q = Quantity("albums.rows[].tracks", "albums", "rows[].tracks", "sum_rows")
    assert resolve(q, complete) == 21.0


def test_strict_is_the_default_and_can_be_relaxed():
    q = Quantity("albums.rows[].tracks", "albums", "rows[].tracks", "sum_rows")
    with pytest.raises(DerivedError):
        resolve(q, TRUNCATED)
    assert resolve(q, TRUNCATED, strict=False) == 21.0


def test_a_derived_metric_over_a_truncated_list_is_refused():
    m = _metric(name="tracks_per_album",
                numerator="albums.rows[].tracks",
                denominator="over.tracks_sold")
    with pytest.raises(DerivedError):
        evaluate(m, TRUNCATED)


def test_a_scalar_from_the_same_truncated_probe_is_still_fine():
    """The denominator 347 is a real total; only the row SUM is partial."""
    q = Quantity("albums.total_in_source", "albums", "total_in_source", "scalar")
    assert resolve(q, TRUNCATED) == 347.0


def test_matching_hint_also_catches_truncation():
    obs = {"c": {"count": 59, "matching": 13,
                 "rows": [{"spend": 49.62}]}}
    q = Quantity("c.rows[].spend", "c", "rows[].spend", "sum_rows")
    with pytest.raises(DerivedError) as e:
        resolve(q, obs)
    # 'count' is checked before 'matching' and both prove truncation
    assert "count=59" in str(e.value)
