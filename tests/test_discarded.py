"""The `discarded` ledger — accounting for what an answer threw away.

Two silent-discard vectors, both of which produced a plausible WRONG answer
that looked internally consistent:

  * a FILTER named in the question that no extracted arg consumed
    ("who spends the most on jazz" -> the unfiltered top customers)
  * ROWS excluded by the probe itself
    (1418 rows with no date: counted by the grand total, absent from every
     group of the breakdown — the parts did not reconcile with the whole,
     and nothing said so)

The ledger is the single mechanism that covers both, and every future member
of the family.
"""
import os
import sys
import textwrap

import pytest

from neuralosd import probe as probe_dec
from neuralosd._cmd import init as initmod
from neuralosd.router import (MEASURE_ARG_NAMES, NoResults, Router,
                              _storage_mask)


# ── generated bridge: row accounting ───────────────────────────────────────

CSV_OK = """\
    order_date,region,sales
    2023-01-05,North,100
    2023-06-05,South,200
    2024-01-05,North,300
    2024-06-05,East,400
"""

# One row has NO date: it still counts towards `total`, but before the ledger
# it vanished from the year breakdown without a word.
CSV_GAP = """\
    order_date,region,sales
    2023-01-05,North,100
    2023-06-05,South,200
    ,North,300
"""


def _build(tmp_path, csv_text, name="gen"):
    src = tmp_path / "src.csv"
    src.write_text(textwrap.dedent(csv_text), encoding="utf-8")
    out = tmp_path / name
    out.mkdir()

    class A:
        pass

    a = A()
    a.source = str(src)
    a.out = str(out)
    a.name = "gen"
    initmod.run(a)
    return str(out)


def _probes(out):
    sys.path.insert(0, out)
    for mod in ("probes", "bridge"):
        sys.modules.pop(mod, None)
    return __import__("probes").PROBES


def test_breakdown_accounts_for_every_row(tmp_path):
    out = _build(tmp_path, CSV_OK)
    sys.path.insert(0, out)
    for mod in ("probes", "bridge"):
        sys.modules.pop(mod, None)
    try:
        bridge = __import__("bridge")
        r = bridge.breakdown("year", "sales")
        assert r["rows_in"] == 4
        assert r["rows_counted"] == 4
        # rows_in == rows_counted + sum(skipped) is the invariant.
        assert r["rows_in"] == r["rows_counted"] + sum(r.get("skipped", {}).values())
        assert r["total"] == 1000.0
        assert r["grand_total"] == 1000.0
        assert r["unaccounted"] == 0.0
    finally:
        sys.path.remove(out)


def test_breakdown_exposes_rows_dropped_for_a_missing_dimension(tmp_path):
    """THE regression: the parts must reconcile with the whole, loudly."""
    out = _build(tmp_path, CSV_GAP)
    sys.path.insert(0, out)
    for mod in ("probes", "bridge"):
        sys.modules.pop(mod, None)
    try:
        bridge = __import__("bridge")
        r = bridge.breakdown("year", "sales")
        assert r["rows_in"] == 3
        assert r["rows_counted"] == 2
        assert r["skipped"] == {"missing_year": 1}     # not silent any more
        assert r["total"] == 300.0                    # accounted for
        assert r["grand_total"] == 600.0              # the truth over all rows
        assert r["unaccounted"] == 300.0              # the divergence, surfaced
        assert r["rows_in"] == r["rows_counted"] + sum(r["skipped"].values())
    finally:
        sys.path.remove(out)


def test_total_reports_non_numeric_skips(tmp_path):
    csv = "region,sales\nNorth,100\nSouth,\nEast,\n"
    out = _build(tmp_path, csv)
    sys.path.insert(0, out)
    for mod in ("probes", "bridge"):
        sys.modules.pop(mod, None)
    try:
        bridge = __import__("bridge")
        r = bridge.total("sales")
        assert r["sum"] == 100.0
        assert r["rows_in"] == 3
        assert r["rows_counted"] == 1
        assert r["skipped"] == {"non_numeric": 2}
        assert r["rows_in"] == r["rows_counted"] + sum(r["skipped"].values())
    finally:
        sys.path.remove(out)


def test_distinct_reports_nulls_and_truncation(tmp_path):
    csv = "region\n" + "\n".join(f"r{i}" for i in range(12)) + "\n,\n"
    out = _build(tmp_path, csv)
    sys.path.insert(0, out)
    for mod in ("probes", "bridge"):
        sys.modules.pop(mod, None)
    try:
        bridge = __import__("bridge")
        r = bridge.distinct("region", cap=5)
        assert r["count"] == 12
        assert len(r["distinct"]) == 5
        assert r["truncated"] == 7                    # the cap, made visible
        assert r["skipped"] == {"null": 1}
    finally:
        sys.path.remove(out)


# ── router: unconsumed FILTER values ───────────────────────────────────────

def _menu():
    def total_sales():
        return {"column": "sales", "sum": 1000, "rows_in": 5, "rows_counted": 5,
                "skipped": {"non_numeric": 0}}

    def top_customers(genre="all"):
        return {"genre": genre, "rows": [{"name": "Ada", "spend": 900}]}

    flat = probe_dec(description="Total (sum) of sales",
                     triggers=["total sales", "sum of sales"],
                     name="total_sales")(total_sales)
    top = probe_dec(
        description="Top customers by spend",
        triggers=["who spends the most", "top customers", "biggest spenders"],
        args={"genre": {"type": "enum", "values": ["jazz", "rock", "blues"],
                        "required": False, "default": "all"}},
        name="top_customers")(top_customers)
    return [flat, top]


def _router(tmp_path, **kw):
    return Router(probes=_menu(),
                  cache_file=str(tmp_path / ".ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"), **kw)


def test_measure_names_are_never_treated_as_filters(tmp_path):
    """'sales' is a measure. Naming it must not be reported as a dropped filter."""
    vocab = _router(tmp_path)._filter_vocab
    assert "jazz" in vocab
    assert "sales" not in vocab


def test_measure_arg_names_are_recognised():
    assert "measure" in MEASURE_ARG_NAMES
    assert "metric" in MEASURE_ARG_NAMES


def test_unconsumed_filter_is_reported(tmp_path):
    """The bug: the filter is named, nothing consumes it, the answer ignores it."""
    env = _router(tmp_path).ask("total sales for jazz")
    assert env["probe"] == "total_sales"
    assert env["discarded"]["terms"] == ["jazz"]


def test_consumed_filter_is_not_reported(tmp_path):
    env = _router(tmp_path).ask("who spends the most on jazz")
    assert env["probe"] == "top_customers"
    assert env["results"][0]["genre"] == "jazz"
    assert "discarded" not in env


def test_no_filter_named_means_no_ledger(tmp_path):
    env = _router(tmp_path).ask("total sales")
    assert env["probe"] == "total_sales"
    assert "discarded" not in env


# ── routing: a probe that ignores a named value must lose ──────────────────

def _routing_menu():
    def total_revenue():
        return {"column": "revenue", "sum": 5350.0}

    def breakdown(dimension, measure):
        return {"by": dimension, "measure": measure, "groups": 1,
                "rows": [{"region": "North", "revenue": 1450.0}],
                "total": 1450.0}

    flat = probe_dec(description="Total (sum) of revenue",
                     triggers=["total revenue", "sum of revenue",
                               "overall revenue"],
                     name="total_revenue")(total_revenue)
    grp = probe_dec(
        description="Break a measure down by a dimension",
        triggers=["breakdown", "by region", "region breakdown",
                  "breakdown by region", "by product"],
        args={"dimension": {"type": "enum",
                            "values": ["region", "product"],
                            "required": True},
              "measure": {"type": "enum", "values": ["revenue", "units"],
                          "required": False, "default": "revenue"}},
        name="breakdown")(breakdown)
    return [flat, grp]


def _routing_router(tmp_path, **kw):
    return Router(probes=_routing_menu(),
                  cache_file=str(tmp_path / ".ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"), **kw)


def test_the_unconsumed_penalty_outweighs_trigger_breadth():
    from neuralosd.router import ARG_MATCH_BONUS, UNCONSUMED_PENALTY
    # 17.6 - 5.0 = 12.6 must be less than 9.6 + 2*3.0 = 15.6
    assert UNCONSUMED_PENALTY > 4.0
    assert ARG_MATCH_BONUS == 3.0


def test_a_named_dimension_routes_to_the_breakdown_not_the_flat_total(tmp_path):
    """THE regression: 'total revenue by region' returned ONE grand total.

    total_revenue scored 17.6 on the trigger "total revenue"; breakdown only
    9.6. But breakdown could CONSUME 'region' and total_revenue could not, so
    the flat probe must lose.
    """
    env = _routing_router(tmp_path).ask("total revenue by region")
    assert env["probe"] == "breakdown", "the flat total won and dropped 'region'"
    assert "discarded" not in env, "nothing should have been dropped"


def test_the_same_question_without_a_dimension_still_hits_the_flat_total(tmp_path):
    env = _routing_router(tmp_path).ask("total revenue")
    assert env["probe"] == "total_revenue"


def test_the_generator_does_not_emit_impossible_triggers(tmp_path):
    """'region over region' can never match a real question."""
    out = _build(tmp_path, CSV_OK)
    text = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "region over region" not in text
    assert "product over product" not in text


def test_a_date_dimension_keeps_year_over_year(tmp_path):
    """'year over year' IS a real phrase, so it must survive."""
    out = _build(tmp_path, CSV_OK)
    text = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "year over year" in text


def test_row_skips_surface_in_the_envelope(tmp_path):
    """A probe that dropped rows puts them in the ledger, not on the floor."""
    def lossy():
        return {"sum": 300, "rows_in": 3, "rows_counted": 2,
                "skipped": {"missing_year": 1, "non_numeric": 0}}

    p = probe_dec(description="Total sales", triggers=["total sales"],
                  name="total_sales")(lossy)
    r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
               audit_file=str(tmp_path / "a.jsonl"))
    env = r.ask("total sales")
    assert env["discarded"]["rows"] == {"missing_year": 1}
    assert "non_numeric" not in env["discarded"]["rows"]   # zero is not a discard


def test_zero_skips_do_not_create_a_ledger(tmp_path):
    def clean():
        return {"sum": 300, "rows_in": 3, "rows_counted": 3,
                "skipped": {"non_numeric": 0}}

    p = probe_dec(description="Total sales", triggers=["total sales"],
                  name="total_sales")(clean)
    r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
               audit_file=str(tmp_path / "a.jsonl"))
    assert "discarded" not in r.ask("total sales")


# ── policy: strict refusal, and never caching a partial answer ─────────────

def test_strict_mode_refuses_rather_than_answering_a_different_question(tmp_path):
    r = _router(tmp_path, strict_discards=True)
    with pytest.raises(NoResults) as e:
        r.ask("total sales for jazz")
    env = e.value.envelope
    assert env["results"] is None
    assert "jazz" in env["error"]


def test_strict_mode_still_answers_a_clean_question(tmp_path):
    r = _router(tmp_path, strict_discards=True)
    env = r.ask("total sales")
    assert env["results"] is not None


def test_partial_answer_is_never_cached(tmp_path):
    """Memoizing a partial answer as if it were complete poisons the TTL."""
    r = _router(tmp_path)
    r.ask("total sales for jazz")
    assert not os.path.exists(r.cache_file), "a partial answer was cached"

    # ...but a clean answer still caches.
    r.ask("total sales")
    assert os.path.exists(r.cache_file)


def test_the_ledger_reaches_the_audit_log(tmp_path):
    r = _router(tmp_path)
    r.ask("total sales for jazz")
    line = open(r.audit_file, encoding="utf-8").read().strip().splitlines()[-1]
    assert '"terms": ["jazz"]' in line.replace("'", '"') or "jazz" in line


def test_discarded_ledger_is_masked_at_storage(tmp_path):
    """The ledger quotes the user's words, so it must go through storage masking."""
    assert _storage_mask({"terms": ["a@b.com"]}) == {"terms": ["***masked***"]}
