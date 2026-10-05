"""The calibrated refusal gate — three outcomes instead of always-answer.

A confidently wrong answer is worse than a refusal: one costs a rephrasing,
the other can be quoted in a board pack. These tests pin the four thresholds
(floor, margin, domain-noun coverage, dropped qualifier), the entity bypass,
and the audit shape.
"""
import json
import os

import pytest

from neuralosd._cmd._common import load_instance
from neuralosd.derived import DerivedMetric
from neuralosd.reasoning import install
from neuralosd.probe import probe as probe_dec
from neuralosd.router import GENERIC_INTENT, NoResults, Router, tokens


def _probe(name, triggers, description="d", args=None, conf_gate=None):
    def fn(**kw):
        return {"probe": name, "ok": True}
    fn.__name__ = name
    return probe_dec(description=description, triggers=triggers, args=args,
                     name=name, conf_gate=conf_gate)(fn)


def _router(tmp_path, probes, **kw):
    return Router(probes=probes, cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"), **kw)


INC = _probe("open_incidents",
             ["open incidents", "how many incidents", "incident count"],
             description="Count of open incidents")
WORKLOG = _probe("worklog_count", ["how many worklog entries exist",
                                   "worklog entry count"],
                 description="Worklog entries in the worklog table")
LOOKUP = _probe("incident_lookup", ["status of incident"],
                description="One incident by reference",
                args={"ref": {"type": "pattern",
                              "pattern": r"INC\d+"}})


def test_floor_refuses_a_generic_prefix_match(tmp_path):
    """'how many' alone matched a probe that knows nothing about worklogs."""
    r = _router(tmp_path, [INC])
    with pytest.raises(NoResults) as e:
        r.ask("how many worklog entries exist")
    assert e.value.envelope["refusal_reason"] == "no_probe_matches"


def test_floor_boundary(tmp_path):
    p = _probe("widgets", ["widget"], description="widget things")
    r = _router(tmp_path, [p], floor=4.0)
    # one token hit -> score 4.0 + breadth: at the floor, answers
    env = r.ask("widget", use_cache=False)
    assert env["probe"] == "widgets"
    r2 = _router(tmp_path, [p], floor=99.0)
    with pytest.raises(NoResults) as e:
        r2.ask("widget", use_cache=False)
    assert e.value.envelope["refusal_reason"] == "below_floor"


def test_margin_tie_is_ambiguous_only_without_a_coverage_advantage(tmp_path):
    """'top genres' ties on the generic token 'top'; the winner knows 'genres'."""
    top_genres = _probe("top_genres", ["top genres", "genre ranking"],
                        description="Genres ranked")
    top_customers = _probe("top_customers", ["top customers"],
                           description="Customers ranked")
    r = _router(tmp_path, [top_genres, top_customers], margin=8.0)
    env = r.ask("top genres", use_cache=False)          # winner covers more
    assert env["probe"] == "top_genres"


def test_a_true_tie_refuses_as_ambiguous(tmp_path):
    a = _probe("open_incidents", ["how many incidents", "incident count"],
               description="open incident count")
    b = _probe("unresolved_incidents", ["how many incidents",
                                        "unresolved count"],
               description="open incident count")
    r = _router(tmp_path, [a, b], margin=2.0)
    with pytest.raises(NoResults) as e:
        r.ask("how many incidents", use_cache=False)    # ties on both tokens
    assert e.value.envelope["refusal_reason"] == "ambiguous"


def test_domain_noun_unknown_to_the_whole_menu_refuses(tmp_path):
    """'worklog' appears in no probe's vocabulary: the subject is out of scope."""
    r = _router(tmp_path, [INC])
    with pytest.raises(NoResults) as e:
        r.ask("worklog entries this week", use_cache=False)
    env = e.value.envelope
    assert env["refusal_reason"] == "no_probe_matches"
    assert any("worklog" in json.dumps(s) or s["score"] == 0
               for s in env["scores"]) or env["scores"] == []


def test_a_known_domain_noun_reaches_its_probe(tmp_path):
    """A domain noun inside the winner's triggers routes; the menu-subject
    check only fires when NO probe knows the noun."""
    genre = _probe("genres", ["genres", "genre catalog"], description="All genres")
    other = _probe("other", ["unrelated"], description="other")
    r = _router(tmp_path, [genre, other])
    assert r.ask("genres", use_cache=False)["probe"] == "genres"


def test_the_menu_subject_check_is_plural_insensitive():
    """'genres' must count as known when a probe is triggered on 'genre'."""
    from neuralosd.router import Router
    r = Router(probes=[_probe("g", ["genre catalog"], description="genres")],
               cache_file="/tmp/_t.json", audit_file="/tmp/_t.jsonl")
    assert r._known_anywhere("genres")
    assert r._known_anywhere("genre")


def test_dropped_qualifier_refuses(tmp_path):
    """'blocked' is a status the winner will silently ignore."""
    wo = _probe("open_work_orders", ["open work orders", "work orders"],
                description="Open work orders")
    r = _router(tmp_path, [wo])
    with pytest.raises(NoResults) as e:
        r.ask("how many work orders are blocked", use_cache=False)
    assert e.value.envelope["refusal_reason"] in ("dropped_filter",
                                                  "no_probe_matches")


def test_a_known_qualifier_does_not_refuse(tmp_path):
    closed = _probe("closed_incidents",
                    ["closed incidents", "incidents closed"],
                    description="Incidents closed")
    r = _router(tmp_path, [closed])
    env = r.ask("how many incidents are closed", use_cache=False)
    assert env["probe"] == "closed_incidents"


def test_an_extracted_entity_bypasses_the_thresholds(tmp_path):
    """A caged id/email/status extraction IS the confidence signal."""
    r = _router(tmp_path, [LOOKUP], floor=99.0, margin=99.0,
                min_question_coverage=0.99)
    env = r.ask("what is the status of incident INC000000077173",
                use_cache=False)
    assert env["probe"] == "incident_lookup"


def test_conf_gate_raises_a_probe_floor(tmp_path):
    shy = _probe("shy", ["widget things"], description="d", conf_gate=50.0)
    r = _router(tmp_path, [shy], floor=3.0)
    with pytest.raises(NoResults) as e:
        r.ask("widget things", use_cache=False)      # scores ~12 < 50
    assert e.value.envelope["refusal_reason"] == "below_floor"


def test_comparative_disjunction_without_a_comparison_probe(tmp_path):
    a = _probe("open_problems", ["open problems"], description="problems")
    wo = _probe("open_work_orders", ["open work orders"], description="orders")
    r = _router(tmp_path, [a, wo])
    with pytest.raises(NoResults) as e:
        r.ask("are there more open problems or open work orders",
              use_cache=False)
    assert e.value.envelope["refusal_reason"] == "comparative"


def test_a_comparison_probe_answers_a_comparative_question(tmp_path):
    cmp_probe = _probe("compare_counts",
                       ["compare counts", "problems versus work orders"],
                       description="Compare problem versus work order counts")
    a = _probe("open_problems", ["open problems"], description="problems")
    r = _router(tmp_path, [cmp_probe, a])
    env = r.ask("compare open problems versus work orders", use_cache=False)
    assert env["probe"] == "compare_counts"


# ── the refusal envelope and its audit ─────────────────────────────────────

def test_refusal_envelope_carries_reason_and_score_vector(tmp_path):
    r = _router(tmp_path, [INC])
    with pytest.raises(NoResults) as e:
        r.ask("how many worklog entries exist", use_cache=False)
    env = e.value.envelope
    assert env["refusal_reason"] in ("no_probe_matches", "low_coverage",
                                     "below_floor", "ambiguous")
    assert isinstance(env["scores"], list)
    for entry in env["scores"]:
        assert set(entry) == {"probe", "score"}


def test_refusals_are_audited_with_the_score_vector(tmp_path):
    r = _router(tmp_path, [INC])
    with pytest.raises(NoResults):
        r.ask("how many worklog entries exist", use_cache=False)
    last = json.loads(open(r.audit_file, encoding="utf-8")
                      .read().strip().splitlines()[-1])
    assert last["refusal_reason"] in ("no_probe_matches", "low_coverage",
                                      "below_floor", "ambiguous")
    assert isinstance(last["scores"], list)
    assert last["results"] is None


def test_refusals_are_never_cached(tmp_path):
    r = _router(tmp_path, [INC])
    with pytest.raises(NoResults):
        r.ask("how many worklog entries exist", use_cache=False)
    assert not os.path.exists(r.cache_file)



# ── threshold plumbing ─────────────────────────────────────────────────────

def test_thresholds_are_per_router_configurable(tmp_path):
    p = _probe("widgets", ["widget"], description="widget")
    strict = _router(tmp_path, [p], floor=99.0)
    loose = _router(tmp_path, [p], floor=0.0)
    with pytest.raises(NoResults):
        strict.ask("widget", use_cache=False)
    assert loose.ask("widget", use_cache=False)["probe"] == "widgets"


def test_router_json_overrides_are_read(tmp_path, monkeypatch):
    (tmp_path / "router.json").write_text(json.dumps({"floor": 99.0}),
                                          encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    p = _probe("widgets", ["widget"], description="widget")
    r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
               audit_file=str(tmp_path / "a.jsonl"))
    assert r.floor == 99.0
    with pytest.raises(NoResults):
        r.ask("widget", use_cache=False)


def test_generic_intent_words_are_excluded_from_domain_nouns():
    assert {"how", "many"} <= GENERIC_INTENT
    assert "worklog" not in GENERIC_INTENT


# ── end-to-end: the real confident-wrong, on the real chinook instance ─────

REAL_CHINOOK = "/Users/olu/neuralos-instances/chinook"


@pytest.mark.skipif(not os.path.isdir(REAL_CHINOOK),
                    reason="chinook instance not present")
def test_chinook_refuses_the_worklog_question(inst=None):
    """The regression that motivated this: chinook has NO worklog data, and the
    router used to answer confidently with table row counts."""
    inst = load_instance(REAL_CHINOOK)
    with pytest.raises(NoResults) as e:
        inst.ask("how many worklog entries exist", use_cache=False)
    env = e.value.envelope
    assert env["refusal_reason"] in ("no_probe_matches", "low_coverage",
                                     "ambiguous")
    assert env["results"] is None


# ── imperative action verbs: the read-only contract ────────────────────────

def test_an_imperative_action_unknown_to_the_menu_refuses(tmp_path):
    """'play the rock music' extracts genre=Rock cleanly - but the user asked
    for an ACTION no probe performs. The extracted enum is evidence of subject,
    not of permission, so the refusal sits before the entity bypass."""
    tracks = _probe("tracks_by_genre", ["tracks by genre", "rock music"],
                    description="Tracks in one genre",
                    args={"genre": {"type": "enum",
                                    "values": ["Rock", "Jazz", "Metal"]}})
    r = _router(tmp_path, [tracks])
    with pytest.raises(NoResults) as e:
        r.ask("play the rock music", use_cache=False)
    assert e.value.envelope["refusal_reason"] == "action_intent"


def test_an_action_verb_a_probe_claims_still_answers(tmp_path):
    player = _probe("play_logs", ["play", "play logs"], description="play logs")
    r = _router(tmp_path, [player])
    assert r.ask("play the logs", use_cache=False)["probe"] == "play_logs"


def test_the_action_check_is_first_word_only(tmp_path):
    """'get the played tracks' - a mid-sentence action word must not refuse a
    legitimate data question."""
    played = _probe("played_tracks", ["played tracks", "played"],
                    description="played tracks")
    r = _router(tmp_path, [played])
    assert r.ask("get the played tracks", use_cache=False)["probe"] == \
        "played_tracks"
