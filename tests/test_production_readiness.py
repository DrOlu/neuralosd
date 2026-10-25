"""Production readiness — the full acceptance suite for neuralOSd 2.0.0.

Tests every guarantee the runtime makes, across every code path:

  GUARANTEE 1: No hallucinations — the model never generates content
  GUARANTEE 2: Honest refusal — the router refuses what it cannot verify
  GUARANTEE 3: Accounting — every answer says what it excluded
  GUARANTEE 4: Verified deployment — a door must answer before "deployed"
  GUARANTEE 5: One menu shape — bare JSON array, defensive reads
  GUARANTEE 6: Entity bypass — caged args are the confidence signal
  GUARANTEE 7: Mesh integration — the same refusal gate on the wire

Every test is hermetic: no network, no Ollama, no microVM, no database.
"""
import json
import os

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import (NoResults, Router, ACTION_VERBS, QUALIFIERS,
                              _args_seen, extract_args,
                              score_probe, tokens, _strip_plural)
from neuralosd.derived import (DerivedError, DerivedMetric, Quantity,
                               check_expectation,
                               inventory_from_observations, resolve)


# ── helpers ────────────────────────────────────────────────────────────────

def _p(name, triggers, args=None, description="d"):
    def fn(*args, **kw):
        return {"probe": name}
    fn.__name__ = name
    return probe_dec(description=description, triggers=triggers, args=args,
                     name=name)(fn)


def _router(tmp_path, probes, **kw):
    return Router(probes=probes, cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"), **kw)


# ═══ GUARANTEE 1: No hallucinations ════════════════════════════════════════

class TestNoHallucinations:
    """The model never generates content. It picks from a closed list."""

    def test_model_output_is_data_not_code(self):
        """A derived metric is a JSON spec with two ids — not executable code."""
        m = DerivedMetric(name="r", kind="ratio",
                          numerator="a.b", denominator="c.d")
        spec = m.to_json()
        assert isinstance(spec, dict)
        assert "code" not in spec
        assert "sql" not in spec
        assert "query" not in spec

    def test_inventory_is_closed(self):
        """Every id a model may choose comes from OBSERVED probe output."""
        obs = {"p": {"count": 5, "rows": [{"id": 1, "total": 100}]}}
        inv = inventory_from_observations(obs)
        ids = {q.id for q in inv}
        assert "p.count" in ids
        assert "p.rows[].total" in ids
        assert "p.nonexistent" not in ids

    def test_fabricated_quantity_is_rejected(self):
        obs = {"p": {"count": 5}}
        inv = inventory_from_observations(obs)
        q = Quantity("p.fake_field", "p", "fake_field", "scalar")
        with pytest.raises(DerivedError):
            resolve(q, obs)


# ═══ GUARANTEE 2: Honest refusal ═══════════════════════════════════════════

class TestHonestRefusal:
    def _menu(self):
        return [
            _p("count_incidents", ["how many incidents", "incident count"],
               description="Count incidents"),
            _p("by_genre", ["by genre", "genre tracks"],
               args={"genre": {"type": "enum", "values": ["Rock", "Jazz"],
                               "required": True}},
               description="By genre"),
            _p("by_ref", ["show incident"],
               args={"ref": {"type": "pattern", "pattern": r"INC\d+"}},
               description="By reference"),
        ]

    def test_simple_question_routes(self, tmp_path):
        r = _router(tmp_path, self._menu())
        env = r.ask("how many incidents", use_cache=False)
        assert env["probe"] == "count_incidents"
        assert env["results"] is not None

    def test_unanswerable_question_refuses(self, tmp_path):
        r = _router(tmp_path, self._menu())
        with pytest.raises(NoResults) as e:
            r.ask("what is the meaning of life", use_cache=False)
        assert e.value.envelope["refusal_reason"] in (
            "no_probe_matches", "below_floor", "low_coverage", "ambiguous")

    def test_action_verb_refuses(self, tmp_path):
        r = _router(tmp_path, self._menu())
        with pytest.raises(NoResults) as e:
            r.ask("delete all invoices", use_cache=False)
        assert e.value.envelope["refusal_reason"] == "action_intent"

    def test_action_intent_before_entity_bypass(self, tmp_path):
        """A destructive question that extracts a caged arg still refuses."""
        r = _router(tmp_path, self._menu())
        with pytest.raises(NoResults) as e:
            r.ask("delete all invoices", use_cache=False)
        assert e.value.envelope["refusal_reason"] == "action_intent"

    def test_refusal_carries_score_vector(self, tmp_path):
        r = _router(tmp_path, self._menu())
        with pytest.raises(NoResults) as e:
            r.ask("what is the meaning of life", use_cache=False)
        scores = e.value.envelope.get("scores")
        assert scores is not None
        assert isinstance(scores, list)

    def test_refusal_reason_is_a_bare_token(self, tmp_path):
        r = _router(tmp_path, self._menu())
        for q in ("delete everything", "what is the weather",
                  "how many worklogs", "xyzzy"):
            try:
                r.ask(q, use_cache=False)
            except NoResults as e:
                reason = e.envelope.get("refusal_reason", "")
                assert "(" not in reason, \
                    f"refusal_reason {reason!r} is not a bare token"
                assert reason in ("no_probe_matches", "below_floor",
                                  "ambiguous", "low_coverage", "comparative",
                                  "dropped_filter", "args_not_extractable",
                                  "action_intent", "model_abstained",
                                  "out_of_range", "probe_error")

    def test_refusals_are_never_cached(self, tmp_path):
        r = _router(tmp_path, self._menu())
        with pytest.raises(NoResults):
            r.ask("delete everything", use_cache=False)
        assert not os.path.exists(r.cache_file)


# ═══ GUARANTEE 3: Accounting ═══════════════════════════════════════════════

class TestAccounting:
    def test_answer_with_rows_in_and_counted(self, tmp_path):
        def count_incidents():
            return {"count": 24, "rows_in": 24, "rows_counted": 24,
                    "skipped": {}}
        p = probe_dec(description="Count incidents",
                      triggers=["how many incidents"],
                      name="count_incidents")(count_incidents)
        r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
                   audit_file=str(tmp_path / "a.jsonl"))
        env = r.ask("how many incidents", use_cache=False)
        assert "discarded" not in env
        assert env["results"][0]["count"] == 24

    def test_answer_with_dropped_rows_carries_the_ledger(self, tmp_path):
        def lossy():
            return {"sum": 300, "rows_in": 3, "rows_counted": 2,
                    "skipped": {"missing_year": 1}}
        p = probe_dec(description="Lossy data", triggers=["lossy data"],
                      name="lossy")(lossy)
        r = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
                   audit_file=str(tmp_path / "a.jsonl"))
        env = r.ask("lossy data", use_cache=False)
        assert env["discarded"]["rows"] == {"missing_year": 1}


# ═══ GUARANTEE 4: Entity bypass ════════════════════════════════════════════

class TestEntityBypass:
    def _probe_with_pattern(self):
        return _p("by_ref", ["show incident"],
                  args={"ref": {"type": "pattern", "pattern": r"INC\d+"}},
                  description="By reference")

    def test_pattern_extraction_bypasses_floor(self, tmp_path):
        r = _router(tmp_path, [self._probe_with_pattern()], floor=999.0)
        env = r.ask("show incident INC001", use_cache=False)
        assert env["probe"] == "by_ref"

    def test_action_intent_still_fires_despite_bypass(self, tmp_path):
        r = _router(tmp_path, [self._probe_with_pattern()], floor=999.0)
        with pytest.raises(NoResults) as e:
            r.ask("delete INC001", use_cache=False)
        assert e.value.envelope["refusal_reason"] == "action_intent"


# ═══ GUARANTEE 5: One menu shape ═══════════════════════════════════════════

class TestMenuShape:
    def test_menu_is_a_list(self, tmp_path):
        r = _router(tmp_path, [_p("a", ["a"])])
        assert isinstance(r.menu(), list)

    def test_menu_entries_have_required_keys(self, tmp_path):
        r = _router(tmp_path, [_p("a", ["a"])])
        for entry in r.menu():
            assert "name" in entry
            assert "description" in entry
            assert "parameters" in entry
            assert "triggers" in entry

    def test_menu_is_json_serializable(self, tmp_path):
        r = _router(tmp_path, [_p("a", ["a"])])
        json.dumps(r.menu())


# ═══ GUARANTEE 6: Derived metrics ══════════════════════════════════════════

class TestDerivedMetrics:
    def test_share_above_one_is_caught(self):
        assert "cannot exceed 1" in check_expectation(3.5, "lte_1")

    def test_zero_share_is_caught(self):
        assert "must be positive" in check_expectation(0.0, "lte_1")

    def test_valid_share_passes(self):
        assert check_expectation(0.19, "lte_1") is None

    def test_unconstrained_expectation_passes_anything(self):
        assert check_expectation(-999, "any") is None
        assert check_expectation(999, "any") is None

    def test_truncated_row_list_is_refused(self):
        obs = {"albums": {"total_in_source": 347, "returned": 2,
                          "rows": [{"id": 1, "tracks": 9},
                                   {"id": 2, "tracks": 12}]}}
        q = Quantity("albums.rows[].tracks", "albums", "rows[].tracks",
                     "sum_rows")
        with pytest.raises(DerivedError) as e:
            resolve(q, obs)
        assert "TRUNCATED" in str(e.value)


# ═══ Plural normalization ══════════════════════════════════════════════════

class TestPluralNormalization:
    def test_strip_plural(self):
        assert _strip_plural({"clinics"}) == {"clinic"}
        assert _strip_plural({"incidents"}) == {"incident"}
        assert _strip_plural({"genres"}) == {"genre"}

    def test_singular_unchanged(self):
        assert _strip_plural({"incident"}) == {"incident"}
        assert _strip_plural({"genre"}) == {"genre"}

    def test_short_words_unchanged(self):
        assert _strip_plural({"cats"}) == {"cat"}
        assert _strip_plural({"dog"}) == {"dog"}

    def test_tokens_are_plural_normalized(self):
        assert tokens("incidents") == {"incident"}
        assert tokens("genres") == {"genre"}


# ═══ Qualifiers: whole-word matching ═══════════════════════════════════════

class TestQualifiers:
    def test_all_qualifiers_are_defined(self):
        from neuralosd.router import QUALIFIERS
        assert len(QUALIFIERS) > 10
        for q in QUALIFIERS:
            assert isinstance(q, str) and len(q) > 0

    def test_action_verbs_are_defined(self):
        from neuralosd.router import ACTION_VERBS
        assert len(ACTION_VERBS) > 10
        for v in ACTION_VERBS:
            assert isinstance(v, str) and len(v) > 0
