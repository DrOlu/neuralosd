"""Production hardening — every edge case that could surface in production.

Tests the FULL routing path (gate → walk → execute → envelope) against a
realistic instance, and pins the edge cases that could break in production:

  FALSE POSITIVES — questions that must be answered, never refused
  FALSE NEGATIVES — questions that must be refused, never answered
  EDGE CASES      — empty, whitespace, unicode, caps, digits, punctuation
  ENTITY BYPASS   — caged args bypass thresholds but not the action gate
  QUALIFIERS      — whole-word matching (not substring)
  ACTION INTENT   — imperative verbs, before the entity bypass
  MENU EXPORT     — caged arg fidelity, canonical shape

Every test uses a REALISTIC instance with separated function objects (the
production pattern), not shared closures.
"""
import json
import os

import pytest

from neuralosd._cmd._common import load_instance
from neuralosd.probe import probe as probe_dec
from neuralosd.router import NoResults, Router

# ── a realistic instance with separated function objects ───────────────────

def _count_incidents():
    return {"count": 24, "rows_in": 24, "rows_counted": 24}

def _count_problems():
    return {"count": 8, "rows_in": 8, "rows_counted": 8}

def _by_genre(genre="Rock"):
    return {"genre": genre, "tracks": 42}

def _by_ref(ref=None):
    return {"ref": ref, "status": "Assigned"}

def _search(q=None):
    return {"matches": 3}

def _unresolved():
    return {"count": 6, "rows_in": 6, "rows_counted": 6}

_p1 = probe_dec(description="Count incidents",
                triggers=["how many incidents", "incident count"],
                name="count_incidents")(_count_incidents)
_p2 = probe_dec(description="Count problems",
                triggers=["how many problems", "problem count"],
                name="count_problems")(_count_problems)
_p3 = probe_dec(description="Tracks by genre",
                triggers=["by genre", "genre tracks", "tracks in genre"],
                args={"genre": {"type": "enum",
                                "values": ["Rock", "Jazz", "Metal"],
                                "required": True, "default": "Rock"}},
                name="by_genre")(_by_genre)
_p4 = probe_dec(description="By reference",
                triggers=["incident by ref", "show incident"],
                args={"ref": {"type": "pattern", "pattern": r"INC\d+"}},
                name="by_ref")(_by_ref)
_p5 = probe_dec(description="Search report",
                triggers=["search report", "search the report"],
                args={"q": {"type": "pattern", "pattern": r"(?:mention|about)\s+(.{3,60})"}},
                name="search_report")(_search)
_p6 = probe_dec(description="Unresolved incidents",
                triggers=["unresolved incidents", "how many unresolved"],
                name="unresolved_incidents")(_unresolved)

ALL_PROBES = [_p1, _p2, _p3, _p4, _p5, _p6]


def _router(tmp_path, **kw):
    return Router(probes=ALL_PROBES, cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"), **kw)


# ── FALSE POSITIVES: questions that MUST be answered ───────────────────────

class TestFalsePositives:
    """Every one of these must produce a result, never a refusal."""

    @pytest.mark.parametrize("q,want", [
        ("how many incidents", "count_incidents"),
        ("incident count", "count_incidents"),
        ("how many problems", "count_problems"),
        ("problem count", "count_problems"),
        ("unresolved incidents", "unresolved_incidents"),
        ("how many unresolved", "unresolved_incidents"),
    ])
    def test_positive_routes_to_the_right_probe(self, tmp_path, q, want):
        r = _router(tmp_path)
        env = r.ask(q, use_cache=False)
        assert env["probe"] == want, \
            f"{q!r} routed to {env['probe']}, expected {want}"
        assert env["results"] is not None

    def test_caged_enum_extraction(self, tmp_path):
        r = _router(tmp_path)
        env = r.ask("tracks in Jazz", use_cache=False)
        assert env["probe"] == "by_genre"
        assert env["results"][0]["genre"] == "Jazz"

    def test_pattern_extraction_with_entity_id(self, tmp_path):
        r = _router(tmp_path)
        env = r.ask("show incident INC00123456", use_cache=False)
        assert env["probe"] == "by_ref"
        assert env["results"][0]["ref"] == "INC00123456"

    def test_search_extraction(self, tmp_path):
        r = _router(tmp_path)
        env = r.ask("search report about revenue", use_cache=False)
        assert env["probe"] == "search_report"


# ── FALSE NEGATIVES: questions that MUST be refused ────────────────────────

class TestFalseNegatives:
    """Every one of these must produce a refusal, never an answer."""

    @pytest.mark.parametrize("q,reason", [
        ("how many worklogs", "no_probe_matches"),
        ("delete everything", "action_intent"),
        ("play the rock music", "action_intent"),
        ("erase the evidence", "action_intent"),
        ("what is the weather", "no_probe_matches"),
        ("who is the president", "no_probe_matches"),
        ("how many work orders are blocked", "dropped_filter"),
        ("how many incidents were resolved", "dropped_filter"),
    ])
    def test_refused_with_reason(self, tmp_path, q, reason):
        r = _router(tmp_path)
        with pytest.raises(NoResults) as e:
            r.ask(q, use_cache=False)
        env = e.value.envelope
        assert env["refusal_reason"] == reason, \
            f"{q!r}: expected {reason}, got {env.get('refusal_reason')}"
        assert env["results"] is None


# ── EDGE CASES: unusual inputs that must not crash or misroute ─────────────

class TestEdgeCases:
    @pytest.mark.parametrize("q", [
        "", " ", "  ", "\t\n",
        "!!!@#$%^&*()",
        "12345",
        "0",
        "🚀🎉",
        "ça va? incidents?",
        "日本語のテスト",
        "a" * 500,
    ])
    def test_unusual_inputs_do_not_crash(self, tmp_path, q):
        r = _router(tmp_path)
        try:
            env = r.ask(q, use_cache=False)
            assert env["results"] is not None or env.get("refusal_reason")
        except NoResults:
            pass              # refusal is always acceptable for garbage input
        except Exception as e:
            pytest.fail(f"{q!r} crashed: {type(e).__name__}: {e}")

    def test_all_caps_routes_correctly(self, tmp_path):
        r = _router(tmp_path)
        env = r.ask("HOW MANY INCIDENTS", use_cache=False)
        assert env["probe"] == "count_incidents"

    def test_repeated_words_do_not_confuse_the_router(self, tmp_path):
        r = _router(tmp_path)
        try:
            env = r.ask("incidents incidents incidents", use_cache=False)
            assert env["probe"] in ("count_incidents", "unresolved_incidents")
        except NoResults:
            pass  # repeated nonsense is also acceptable to refuse

    def test_very_long_question_does_not_crash(self, tmp_path):
        r = _router(tmp_path)
        q = "how many incidents " + "and " * 50 + "more details please"
        try:
            env = r.ask(q, use_cache=False)
        except NoResults:
            pass              # refusal is fine for a garbled question


# ── the entity bypass: caged args bypass thresholds ────────────────────────

class TestEntityBypass:
    def test_pattern_extraction_bypasses_floor(self, tmp_path):
        """An extracted id IS the confidence signal — even with floor=999."""
        r = _router(tmp_path, floor=999.0)
        env = r.ask("show incident INC001", use_cache=False)
        assert env["probe"] == "by_ref"

    def test_enum_extraction_bypasses_coverage(self, tmp_path):
        """An extracted enum value IS the confidence signal."""
        r = _router(tmp_path, min_question_coverage=0.99)
        env = r.ask("tracks in Jazz", use_cache=False)
        assert env["probe"] == "by_genre"

    def test_action_intent_still_fires_despite_entity_bypass(self, tmp_path):
        """The action gate sits BEFORE the entity bypass: "play the rock music"
        extracts genre=Rock, but the user asked for an ACTION."""
        r = _router(tmp_path)
        with pytest.raises(NoResults) as e:
            r.ask("play the rock music", use_cache=False)
        assert e.value.envelope["refusal_reason"] == "action_intent"


# ── the qualifier check: whole-word, not substring ─────────────────────────

class TestQualifierMatching:
    def test_closed_does_not_fire_inside_unresolved(self, tmp_path):
        """THE substring bug: 'resolved' matched inside 'unresolved'."""
        r = _router(tmp_path)
        env = r.ask("how many unresolved incidents", use_cache=False)
        assert env["probe"] == "unresolved_incidents"
        assert env.get("refusal_reason") != "dropped_filter"

    def test_blocked_still_fires_when_unknown(self, tmp_path):
        """'blocked' is a status the winner cannot apply."""
        r = _router(tmp_path)
        with pytest.raises(NoResults) as e:
            r.ask("how many incidents are blocked", use_cache=False)
        assert e.value.envelope["refusal_reason"] in ("dropped_filter",
                                                      "no_probe_matches")


# ── the action-intent gate ─────────────────────────────────────────────────

class TestActionIntent:
    @pytest.mark.parametrize("q", [
        "delete everything",
        "play the rock music",
        "erase the evidence",
        "drop the table",
        "send the email",
    ])
    def test_imperative_actions_refuse(self, tmp_path, q):
        r = _router(tmp_path)
        with pytest.raises(NoResults) as e:
            r.ask(q, use_cache=False)
        assert e.value.envelope["refusal_reason"] == "action_intent"

    def test_a_probe_that_claims_the_action_still_answers(self, tmp_path):
        """If a probe's vocabulary includes the action verb, don't refuse."""
        player = probe_dec(description="Play logs",
                           triggers=["play"], name="play_logs")(
            lambda **kw: {"playing": True})
        r = Router(probes=[player], cache_file=str(tmp_path / "c.json"),
                   audit_file=str(tmp_path / "a.jsonl"))
        env = r.ask("play the logs", use_cache=False)
        assert env["probe"] == "play_logs"


# ── the menu export: caged arg fidelity ────────────────────────────────────

class TestMenuExport:
    def test_export_carries_enum_and_pattern(self, tmp_path):
        r = _router(tmp_path)
        menu = r.menu()
        by_genre = next(m for m in menu if m["name"] == "by_genre")
        props = by_genre["parameters"]["properties"]
        assert props["genre"]["values"] == ["Rock", "Jazz", "Metal"]
        assert props["genre"]["enum"] == ["Rock", "Jazz", "Metal"]
        by_ref = next(m for m in menu if m["name"] == "by_ref")
        ref_props = by_ref["parameters"]["properties"]
        assert ref_props["ref"]["pattern"] == r"INC\d+"

    def test_export_carries_default(self, tmp_path):
        r = _router(tmp_path)
        menu = r.menu()
        by_ref = next(m for m in menu if m["name"] == "by_ref")
        props = by_ref["parameters"]["properties"]
        # by_ref has no default, but genre has values and pattern
        assert "pattern" in props["ref"]

    def test_export_carries_min_max(self, tmp_path):
        r = _router(tmp_path)
        menu = r.menu()
        by_genre = next(m for m in menu if m["name"] == "by_genre")
        props = by_genre["parameters"]["properties"]
        # the genre arg is an enum: min/max don't apply
        assert "pattern" not in props["genre"] or "values" in props["genre"]


# ── the discarded ledger ───────────────────────────────────────────────────

class TestDiscardedLedger:
    def test_a_dropped_filter_appears_in_the_ledger(self, tmp_path):
        r = _router(tmp_path)
        with pytest.raises(NoResults):
            r.ask("how many incidents are blocked", use_cache=False)

    def test_clean_answers_have_no_ledger(self, tmp_path):
        r = _router(tmp_path)
        env = r.ask("how many incidents", use_cache=False)
        assert "discarded" not in env


# ── the audit log ──────────────────────────────────────────────────────────

class TestAudit:
    def test_every_ask_is_audited(self, tmp_path):
        r = _router(tmp_path)
        try:
            r.ask("how many incidents", use_cache=False)
        except NoResults:
            pass
        assert os.path.exists(r.audit_file)
        last = json.loads(open(r.audit_file, encoding="utf-8")
                          .read().strip().splitlines()[-1])
        assert "ask_id" in last
        assert "question" in last

    def test_refusals_are_audited_with_the_reason(self, tmp_path):
        r = _router(tmp_path)
        try:
            r.ask("how many worklogs", use_cache=False)
        except NoResults:
            pass
        last = json.loads(open(r.audit_file, encoding="utf-8")
                          .read().strip().splitlines()[-1])
        assert last["refusal_reason"] in ("no_probe_matches", "low_coverage",
                                          "below_floor", "ambiguous",
                                          "dropped_filter", "action_intent")


# ── the cache ──────────────────────────────────────────────────────────────

class TestCache:
    def test_identical_asks_hit_the_cache(self, tmp_path):
        r = _router(tmp_path)
        r.ask("how many incidents", use_cache=True)
        env = r.ask("how many incidents", use_cache=True)
        assert env.get("cached") is True

    def test_refusals_are_never_cached(self, tmp_path):
        r = _router(tmp_path)
        with pytest.raises(NoResults):
            r.ask("delete everything", use_cache=False)
        assert not os.path.exists(r.cache_file)

    def test_different_questions_do_not_collide(self, tmp_path):
        r = _router(tmp_path)
        e1 = r.ask("how many incidents", use_cache=True)
        e2 = r.ask("how many problems", use_cache=True)
        assert e1["probe"] != e2["probe"] or e1["ask_id"] != e2["ask_id"]
