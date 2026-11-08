"""Every refusal row must carry the SAME schema — ts, mode, refusal_reason.

One branch in `Router.ask` ("nothing the deterministic layer would stand
behind and no model fallback configured") built its audit envelope inline and
emitted neither `ts` nor `mode`, while every other refusal path goes through
`_refuse()`. Consequences in production: `loop.refusal_rate()` skips rows
whose `ts` is not numeric, so those asks counted as NEITHER ask NOR refusal
(fleet/slo undercounted), and `ask_audit.jsonl` carried two incompatible row
shapes that every consumer had to special-case.

The parametrization walks every reachable refusal path and asserts the row ON
DISK (the audit line, not just the raised envelope): numeric `ts`,
`mode == "refused"`, a gate-vocabulary `refusal_reason`, `probe is None`, and
the `source` stamp (default "user").
"""
import json

import pytest

from neuralosd import probe as probe_dec
from neuralosd.router import REFUSAL_REASONS, NoResults, Router


def _p(name, triggers, fn, args=None):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     args=args or {}, name=name)(fn)


def _sales_router(tmp_path, args_router=False, strict=False, fallback=None):
    """`args_router=True` gives total_sales a REQUIRED region arg (the
    args-not-extractable path); the default gives it NO args so the probe
    actually executes — a probe with an un-extractable required arg refuses
    before it ever runs, and a dropped filter needs a probe that ran."""
    def total_sales(region=None):
        return [{"region": region, "total": 42}]

    def show_user(user=1):
        return {"user": user, "name": "someone"}

    def genre_breakdown(genre):
        return [{"genre": genre, "n": 1}]

    sales_args = (None if args_router else
                  {"region": {"type": "str", "required": False,
                              "values": ["north", "south"]}})
    required_region = ({"region": {"type": "str", "required": True,
                                   "values": ["north", "south"]}}
                       if args_router else None)
    probes = [
        _p("total_sales", ["total sales"], total_sales, args=sales_args),
        _p("show_user", ["show user"], show_user,
           args={"user": {"type": "integer", "min": 1, "max": 10,
                          "required": False, "default": 1}}),
        # supplies "jazz"/"rock" to the filter vocabulary WITHOUT ever
        # becoming a candidate for "total sales for jazz" (coverage of its
        # own trigger, "genre breakdown", is zero in that question)
        _p("genre_breakdown", ["genre breakdown"], genre_breakdown,
           args={"genre": {"type": "str", "required": True,
                           "values": ["jazz", "rock"]}}),
    ]
    if args_router:
        probes[0] = _p("total_sales", ["total sales"], total_sales,
                       args=required_region)
    return Router(
        probes=probes,
        model_fallback=fallback,
        strict_discards=strict,
        cache_file=str(tmp_path / ".ask_cache.json"),
        audit_file=str(tmp_path / "ask_audit.jsonl"),
    )


def _last_row(tmp_path):
    with open(tmp_path / "ask_audit.jsonl", encoding="utf-8") as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    assert lines, "no audit rows were written"
    return json.loads(lines[-1])


CASES = [
    # (case id, question, kwargs, expected refusal_reason)
    ("no_probe_matched", "xyzzy plugh", {}, "no_probe_matches"),
    ("args_not_extractable", "total sales", {"args": True},
     "args_not_extractable"),
    ("out_of_range_arg", "show user 999", {}, "out_of_range"),
    ("action_intent", "delete all sales", {}, "action_intent"),
    ("dropped_filter", "total sales for jazz",
     {"strict": True}, "dropped_filter"),
    ("model_abstained", "xyzzy plugh",
     {"fallback": lambda q, c: None}, "model_abstained"),
]


@pytest.mark.parametrize("label,question,kw,want_reason", CASES, ids=[c[0] for c in CASES])
def test_every_refusal_path_writes_the_full_schema(tmp_path, label,
                                                   question, kw, want_reason):
    r = _sales_router(tmp_path, args_router=kw.get("args", False),
                      strict=kw.get("strict", False),
                      fallback=kw.get("fallback"))
    with pytest.raises(NoResults) as e:
        r.ask(question)
    env = e.value.envelope
    # the raised envelope AND the audited row must agree on the essentials
    assert env["refusal_reason"] == want_reason
    row = _last_row(tmp_path)
    assert isinstance(row.get("ts"), float), (
        f"{label}: refusal row has no numeric ts — refusal_rate() will skip "
        f"it and the SLO will silently undercount: {row}")
    assert row.get("mode") == "refused", row
    assert row.get("refusal_reason") == want_reason
    assert row.get("probe") is None
    assert row.get("results") is None
    # v2.9.6: every audit row is stamped with its producer
    assert row.get("source") == "user"
    # the reason must stay inside the gate vocabulary, next to GATE_VERSION
    assert want_reason in REFUSAL_REASONS


def test_refusal_row_also_stamps_scores_and_menu_version(tmp_path):
    """The schema pins are not just presence: the row is a usable SLO record."""
    with pytest.raises(NoResults):
        _sales_router(tmp_path).ask("xyzzy plugh")  # no_probe_matches
    row = _last_row(tmp_path)
    assert isinstance(row["scores"], list)
    assert row["menu_version"]
    assert row["ask_id"]
