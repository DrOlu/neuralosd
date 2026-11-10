"""E1 — the routing breadth term must measure DISTINCT coverage, not repetition.

Reproduced on the published v2.9.8 binary: `score_probe`'s breadth term was
`1.0 * sum(overlaps)` — it grew with the NUMBER of triggers, so a probe with
22 variants of "<qualifier> detections" collected ~22 breadth points from the
two-word question "are detections increasing" and outranked (32.3 vs 22.3)
the probe whose single trigger was the question VERBATIM. Live instances
misrouted "are detections increasing" to the detections listing, and ranked
a dispositions probe below the detections probe so the discard gate refused
a question the instance could answer.

The fix counts the DISTINCT question tokens any trigger covers — bounded by
the question length, therefore not gameable by adding synonyms. Everything
else (best at 4.0, exact-phrase bonus 5.0*max(covered), name/description
terms, unconsumed-argument penalty) is deliberately untouched.

These tests fail on v2.9.8 and pass after the fix.
"""
from neuralosd.lint import TriggerLinter
from neuralosd.probe import ProbeMeta
from neuralosd.router import score_probe, tokens


def _mk(name, desc, triggers):
    return ProbeMeta(name=name, description=desc, triggers=triggers, args={},
                     pii=[], conf_gate=None, tier="read", confirm=False,
                     function=lambda: None)


# The fixture shape from the field report: one probe with a pile of
# near-duplicate triggers all anchored on "detections", one probe whose
# single trigger IS the question.
DETECTIONS_WINDOW = [
    f"{q} detections" for q in
    ("recent", "open", "new", "today", "latest", "active", "current",
     "high", "critical", "unresolved", "past", "week", "daily", "all",
     "live", "inflight", "flagged", "watchlist", "ongoing", "top",
     "last", "fresh")
]


class TestBreadthCannotBeGamed:
    def test_intent_question_prefers_the_exact_phrase_probe(self):
        window = _mk("detections_window",
                     "Detections in a time window", DETECTIONS_WINDOW)
        trend = _mk("detection_trend",
                    "Trend of detections over time",
                    ["are detections increasing"])
        q = tokens("are detections increasing")
        s_window, s_trend = score_probe(window, q), score_probe(trend, q)
        # v2.9.8 numbers for this exact fixture: window 32.3, trend 22.3.
        assert s_trend > s_window, (
            "a probe listing the question verbatim must outrank a probe with "
            f"22 trigger-spam variants (trend={s_trend}, window={s_window})")

    def test_dispositions_shape_ranks_exact_probe_first(self):
        # Second field report: "were any detections closed without a
        # disposition" must not rank the generic detections probe first.
        window = _mk("detections_window",
                     "Detections in a time window", DETECTIONS_WINDOW)
        disp = _mk("detection_dispositions",
                   "Detections closed without a disposition",
                   ["detections closed without a disposition"])
        q = tokens("were any detections closed without a disposition")
        assert score_probe(disp, q) > score_probe(window, q)

    def test_how_many_rows_still_prefers_row_count(self):
        # The case the best-term comment cites must keep working: best is
        # untouched, so an exact-phrase probe still dominates.
        rc = _mk("row_count", "Count rows", ["how many rows"])
        lr = _mk("list_rows", "List rows in a table",
                 ["list rows", "show rows", "rows in table"])
        q = tokens("how many rows")
        assert score_probe(rc, q) > score_probe(lr, q)

    def test_breadth_is_bounded_by_question_length(self):
        # Adding more synonyms must NOT move the breadth term: distinct
        # coverage is capped by the question's own token count. Neither
        # probe may hold a fully-covered trigger (that would engage the
        # untouched exact-phrase bonus instead of breadth), so with breadth
        # neutralised the two scores must come out EQUAL.
        q = tokens("are detections increasing")
        few = _mk("p", "d", ["recent detections", "open detections"])
        many_trigs = [t for t in DETECTIONS_WINDOW if t != "all detections"]
        many = _mk("p", "d", many_trigs)
        assert not any(tokens(t) <= q for t in few.triggers)
        assert not any(tokens(t) <= q for t in many.triggers)
        assert score_probe(many, q) == score_probe(few, q), (
            "synonym spam must not grow the breadth term")


class TestLintFlagsTriggerSpam:
    def test_trigger_spam_probe_is_flagged(self):
        menu = [
            {"name": "detections_window", "description": "detections window",
             "triggers": DETECTIONS_WINDOW},
            {"name": "detection_trend", "description": "trend of detections",
             "triggers": ["are detections increasing"]},
        ]
        report = TriggerLinter(menu).lint()
        spam_names = [name for name, _n, _trig in report["trigger_spam"]]
        assert "detections_window" in spam_names, (
            ">=3 variants sharing >=80% of tokens must be flagged")
        assert "detection_trend" not in spam_names

    def test_distinct_triggers_are_not_flagged(self):
        menu = [
            {"name": "mixed", "description": "varied coverage",
             "triggers": ["revenue by region", "units sold per quarter",
                          "shipping cost trends", "profit margin analysis"]},
        ]
        assert TriggerLinter(menu).lint()["trigger_spam"] == []
