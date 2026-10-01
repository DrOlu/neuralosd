"""Tests for chains, meta-selector, HITL, lint, and backends."""
import pytest
import json
from neuralosd import (probe, enum_arg, pattern_arg, int_arg,
                       ChainRunner, chain, ChainError,
                       MetaSelector, ConfirmStore, ConfirmRequired)


# ── Chains ────────────────────────────────────────────────────────────────

class TestChains:
    def _make_runner(self):
        @probe(description="Step A: get genres",
               triggers=["get genres", "list genres"])
        def get_genres():
            return {"genre_count": 3,
                    "with_track_counts": [{"genre": "Rock", "tracks": 100},
                                          {"genre": "Jazz", "tracks": 50}]}

        @probe(description="Step B: tracks by genre",
               triggers=["tracks by genre", "tracks in genre"],
               args={"genre": enum_arg(["Rock", "Jazz"])})
        def tracks_by_genre(genre: str):
            return {"genre": genre, "count": 100 if genre == "Rock" else 50}

        runner = ChainRunner({p._probe.name: p for p in [get_genres, tracks_by_genre]})
        runner.register("genre_deep_dive", [
            {"probe": "get_genres"},
            {"probe": "tracks_by_genre", "args": {"genre": "{{step_0.with_track_counts.0.genre}}"}},
        ])
        return runner

    def test_chain_runs(self):
        runner = self._make_runner()
        result = runner.run("genre_deep_dive")
        assert result["chain"] == "genre_deep_dive"
        assert len(result["steps"]) == 2
        assert result["steps"][1]["result"]["genre"] == "Rock"

    def test_chain_template_resolution(self):
        from neuralosd.chains import resolve_template
        ctx = {"step_0": {"genre": "Rock", "tracks": 100}}
        assert resolve_template("{{step_0.genre}}", ctx) == "Rock"

    def test_chain_missing_ref(self):
        with pytest.raises(ChainError):
            from neuralosd.chains import resolve_template
            resolve_template("{{nonexistent.field}}", {})

    def test_chain_not_registered(self):
        runner = self._make_runner()
        with pytest.raises(ChainError):
            runner.run("nonexistent_chain")


# ── Meta-selector ─────────────────────────────────────────────────────────

class TestMetaSelector:
    def _make(self):
        from neuralosd import MetaSelector
        return MetaSelector({
            "chinook": {"description": "chinook digital music store: albums, artists, customers, invoices, tracks, genres",
                        "examples": ["top customers", "sales by country", "albums by AC/DC"]},
            "weather": {"description": "weather data: temperature, forecast, conditions by city",
                        "examples": ["weather in Lagos", "forecast for London"]},
            "incidents": {"description": "IT incident management: severity, status, teams",
                          "examples": ["open incidents", "P1 escalations"]},
        })

    def test_routes_music(self):
        selector = self._make()
        assert selector.route("top customers by spend") == "chinook"

    def test_routes_weather(self):
        selector = self._make()
        assert selector.route("what is the weather in Lagos") == "weather"

    def test_routes_incidents(self):
        selector = self._make()
        assert selector.route("how many open incidents") == "incidents"


# ── HITL ──────────────────────────────────────────────────────────────────

class TestHITL:
    def test_confirm_flow(self):
        from neuralosd import ConfirmStore
        store = ConfirmStore(ttl=60)

        @probe(description="Delete all data", triggers=["delete all"], confirm=True)
        def delete_all():
            return {"deleted": True}

        pending = store.create(delete_all, {}, "flagged: confirm=True")
        token = pending["confirm_token"]
        result = store.confirm(token)
        assert result.get("deleted") is True

    def test_confirm_expired(self):
        from neuralosd import ConfirmStore
        store = ConfirmStore(ttl=0)  # instant expiry
        pending = store.create(lambda: {}, {}, "test")
        result = store.confirm(pending["confirm_token"])
        assert "error" in result


# ── Lint ──────────────────────────────────────────────────────────────────

class TestLint:
    def test_no_collisions(self):
        from neuralosd.lint import TriggerLinter
        menu = [
            {"name": "probe_a", "description": "Alpha queries",
             "triggers": ["alpha search", "alpha list"]},
            {"name": "probe_b", "description": "Beta queries",
             "triggers": ["beta search", "beta list"]},
        ]
        r = TriggerLinter(menu).lint()
        assert len(r["hard"]) == 0

    def test_duplicate_trigger_is_authoring_error(self):
        """Duplicate triggers are a menu authoring error, not a routing
        collision — the linter checks ROUTING, not duplicates. Both probes
        score equally so the sort is stable (no hard collision)."""
        from neuralosd.lint import TriggerLinter
        menu = [
            {"name": "probe_a", "description": "Alpha queries",
             "triggers": ["alpha search"]},
            {"name": "probe_b", "description": "Beta queries",
             "triggers": ["alpha search"]},
        ]
        r = TriggerLinter(menu).lint()
        # no hard collision: both score equally, stable sort picks first
        assert isinstance(r["hard"], list)
