"""Group-by ("breakdown") probes — generation, execution and routing.

The regression this guards: before, the generated instances only had flat
aggregates, so "sales by quarter" matched `total_sales` and returned ONE
grand total — a confident answer to a question nobody asked. That is the worst
failure mode, so it gets explicit tests.
"""
import datetime
import os
import sys
import textwrap

import pytest

from neuralosd._cmd import init as initmod
from neuralosd.instance import Instance
from neuralosd.router import ARG_MATCH_BONUS, _args_seen, tokens
from neuralosd import probe as probe_dec


# ── the routing principle ──────────────────────────────────────────────────

def test_args_seen_counts_question_words():
    assert _args_seen({"dimension": "market"}, "sales by market") == 1
    # both values appear (case-insensitively): "market" and "Sales" -> "sales"
    assert _args_seen({"dimension": "market", "measure": "Sales"},
                      "sales by market") == 2
    assert _args_seen({"dimension": "region"}, "sales by market") == 0
    assert _args_seen({}, "sales by market") == 0
    assert _args_seen({"x": None}, "anything") == 0
    assert _args_seen({"x": 42}, "anything") == 0      # non-strings ignored


def test_args_seen_is_case_insensitive():
    assert _args_seen({"dimension": "Market"}, "sales by market") == 1


def _breakdown_instance(tmp_path):
    """A menu with both a flat total and a breakdown over the same vocabulary."""

    def total_sales():
        return {"column": "Sales", "sum": 100}

    def breakdown(dimension, measure):
        return {"by": dimension, "measure": measure, "rows": []}

    flat = probe_dec(description="Total (sum) of Sales",
                     triggers=["total sales", "sum sales", "sales total"],
                     name="total_sales")(total_sales)
    grp = probe_dec(
        description="Break a measure down by a dimension (group by)",
        triggers=["by market", "market breakdown", "breakdown by market",
                  "by quarter", "quarter breakdown", "breakdown by quarter"],
        args={"dimension": {"type": "enum",
                            "values": ["market", "quarter"]},
              "measure": {"type": "enum", "values": ["Sales"],
                          "required": False, "default": "Sales"}},
        name="breakdown")(breakdown)
    return Instance(name="t", probes=[flat, grp], state_dir=str(tmp_path))


def test_breakdown_wins_when_the_question_names_a_dimension(tmp_path):
    """REGRESSION: 'sales by quarter' used to return a single grand total."""
    inst = _breakdown_instance(tmp_path)
    env = inst.ask("sales by quarter")
    assert env["probe"] == "breakdown"
    assert env["results"][0]["by"] == "quarter"


def test_breakdown_wins_for_market_too(tmp_path):
    inst = _breakdown_instance(tmp_path)
    env = inst.ask("sales by market")
    assert env["probe"] == "breakdown"
    assert env["results"][0]["by"] == "market"


def test_flat_total_still_wins_without_a_dimension(tmp_path):
    """FALSE POSITIVE guard: a plain total must not become a breakdown."""
    inst = _breakdown_instance(tmp_path)
    env = inst.ask("total sales")
    assert env["probe"] == "total_sales"
    assert env["results"][0]["sum"] == 100


def test_bonus_is_positive_and_meaningful():
    assert ARG_MATCH_BONUS > 0
    assert ARG_MATCH_BONUS >= 3.0        # must outrank mere vocabulary sharing


# ── profiling: date + dimension/measure detection ──────────────────────────

def test_csv_date_column_detected(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("order_date,region,sales\n"
                 "2024-01-05,North,100\n"
                 "2024-02-05,South,200\n", encoding="utf-8")
    info = initmod._profile_delimited(str(p))
    assert info["columns"][0]["type"] == "date"
    c0 = info["columns"][0]
    assert c0["date_column"] == "order_date"
    assert "year" in c0["dimensions"] and "quarter" in c0["dimensions"]
    assert c0["measures"] == ["sales"]


def test_bare_numbers_are_not_dates(tmp_path):
    p = tmp_path / "n.csv"
    p.write_text("year,region,sales\n2024,North,100\n2023,South,200\n",
                 encoding="utf-8")
    info = initmod._profile_delimited(str(p))
    assert info["columns"][0]["type"] != "date"


def test_looks_like_date_rejects_ids_and_accepts_real_dates():
    assert initmod._looks_like_date(["2024-01-05", "2024-02-05"]) is True
    assert initmod._looks_like_date(["05/01/2024", "06/01/2024"]) is True
    assert initmod._looks_like_date(["12345", "67890"]) is False
    assert initmod._looks_like_date([]) is False


def test_measure_detection_skips_ids():
    assert initmod._is_measure({"name": "Sales", "type": "float"}) is True
    assert initmod._is_measure({"name": "Row ID", "type": "int"}) is False
    assert initmod._is_measure({"name": "Postal Code", "type": "int"}) is False
    assert initmod._is_measure({"name": "Region", "type": "str"}) is False


# ── generation ─────────────────────────────────────────────────────────────

def _build_instance(tmp_path, csv_text, name="gen"):
    src = tmp_path / "src.csv"
    src.write_text(textwrap.dedent(csv_text), encoding="utf-8")
    out = tmp_path / name
    out.mkdir()

    class A:
        source = str(src)
        out_ = None
        name = "gen"

    a = A()
    a.out = str(out)
    initmod.run(a)
    return str(out), str(src)


CSV = """\
    order_date,region,product,sales,profit
    2023-01-05,North,Widget,100,10
    2023-06-05,South,Gadget,200,20
    2024-01-05,North,Widget,300,30
    2024-06-05,East,Gadget,400,40
"""


def test_generated_instance_has_a_breakdown_probe(tmp_path):
    out, _ = _build_instance(tmp_path, CSV)
    text = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "def breakdown(" in text
    assert '"dimension"' in text and '"measure"' in text
    assert "year" in text                         # derived date dimension


def test_generated_breakdown_actually_groups_by_year(tmp_path):
    out, _ = _build_instance(tmp_path, CSV)
    sys.path.insert(0, out)
    try:
        inst = Instance(name="gen", probes=__import__("probes").PROBES,
                        state_dir=out)
        env = inst.ask("sales by year")
        assert env["probe"] == "breakdown"
        res = env["results"][0]
        assert res["measure"] == "sales"
        by = {r["year"]: r["sales"] for r in res["rows"]}
        assert by == {"2023": 300.0, "2024": 700.0}
    finally:
        sys.path.remove(out)
        sys.modules.pop("probes", None)


def test_generated_breakdown_groups_by_a_column(tmp_path):
    out, _ = _build_instance(tmp_path, CSV)
    sys.path.insert(0, out)
    try:
        inst = Instance(name="gen", probes=__import__("probes").PROBES,
                        state_dir=out)
        env = inst.ask("profit by region")
        res = env["results"][0]
        assert (res["by"], res["measure"]) == ("region", "profit")
        by = {r["region"]: r["profit"] for r in res["rows"]}
        assert by == {"East": 40.0, "North": 40.0, "South": 20.0}
    finally:
        sys.path.remove(out)
        sys.modules.pop("probes", None)


def test_breakdown_args_are_caged(tmp_path):
    out, _ = _build_instance(tmp_path, CSV)
    sys.path.insert(0, out)
    try:
        probes = __import__("probes").PROBES
        bd = next(p for p in probes if p._probe.name == "breakdown")
        assert bd._probe.args["dimension"]["type"] == "enum"
        assert "year" in bd._probe.args["dimension"]["values"]
        assert bd._probe.args["measure"].get("required") is False
        assert bd._probe.args["measure"].get("default")
    finally:
        sys.path.remove(out)
        sys.modules.pop("probes", None)


def test_no_date_column_still_generates_a_breakdown(tmp_path):
    out, _ = _build_instance(tmp_path, """\
        region,product,sales
        North,Widget,100
        South,Gadget,200
    """)
    text = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "def breakdown(" in text
    assert "quarter" not in text                  # no date column => no date parts


def test_month_and_quarter_grouping_labels(tmp_path):
    """The derived date dimensions produce sensible keys."""
    out, _ = _build_instance(tmp_path, CSV)
    sys.path.insert(0, out)
    try:
        import bridge
        assert bridge._date_part("2024-06-05", "year") == "2024"
        assert bridge._date_part("2024-06-05", "quarter") == "2024-Q2"
        assert bridge._date_part("2024-06-05", "month") == "2024-06"
        assert bridge._date_part(datetime.datetime(2024, 6, 5), "quarter") == "2024-Q2"
        assert bridge._date_part("not a date", "year") is None
        assert bridge._date_part(None, "year") is None
    finally:
        sys.path.remove(out)
        sys.modules.pop("bridge", None)


def test_breakdown_totals_match_the_flat_total(tmp_path):
    """The parts must add up to the whole."""
    out, _ = _build_instance(tmp_path, CSV)
    sys.path.insert(0, out)
    try:
        import bridge
        by_year = bridge.breakdown("year", "sales")
        flat = bridge.total("sales")
        assert by_year["total"] == flat["sum"]
    finally:
        sys.path.remove(out)
        sys.modules.pop("bridge", None)
