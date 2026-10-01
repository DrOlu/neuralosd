"""Unit tests for the router pipeline."""
import pytest
from neuralosd import probe, enum_arg, pattern_arg, int_arg, Router, NoResults


def make_probes():
    @probe(description="Database overview with counts and revenue",
           triggers=["database overview", "how many records",
                     "total revenue", "store overview"])
    def overview(): return {"records": 100, "revenue": 500}

    @probe(description="Search by name",
           triggers=["search by name", "find by name", "name search"],
           args={"name": pattern_arg(r"(?:by|named)\s+(\w+)")})
    def search(name: str): return {"query": name, "matches": 3}

    @probe(description="Filter by country",
           triggers=["in country", "by country", "filter country"],
           args={"country": enum_arg(["USA", "UK", "Nigeria"])})
    def by_country(country: str): return {"country": country, "count": 5}

    @probe(description="Get details by id",
           triggers=["details", "get details", "show details"],
           args={"id": int_arg(1, 99999)})
    def details(id: int): return {"id": id, "data": "..."}

    return [overview, search, by_country, details]


class TestRouter:
    def test_deterministic_enum(self):
        rt = Router(probes=make_probes(), model_fallback=None)
        env = rt.ask("filter country USA")
        assert env["mode"] == "deterministic"
        assert env["results"][0]["country"] == "USA"

    def test_deterministic_pattern(self):
        rt = Router(probes=make_probes(), model_fallback=None)
    def test_deterministic_pattern(self):
        rt = Router(probes=make_probes(), model_fallback=None)
        env = rt.ask("get details 42")
        assert env["mode"] == "deterministic"
        assert env["results"][0]["id"] == 42
        rt = Router(probes=make_probes(), model_fallback=None)
        env = rt.ask("get details 42")
        assert env["mode"] == "deterministic"
        assert "42" in json.dumps(env["results"])

    def test_no_results_raises(self):
        rt = Router(probes=make_probes(), model_fallback=None)
        with pytest.raises(NoResults):
            rt.ask("completely unrelated query about quantum physics")

    def test_model_fallback_called(self):
        calls = []
        def fallback(q, metas):
            return [{"fallback": True, "query": q, "metas": [m.name for m in metas]}]
        rt = Router(probes=make_probes(), model_fallback=fallback)
        # "get details for record 42" retrieves the `details` probe but
        # int extraction succeeds -> deterministic, not fallback. Use a
        # question where enum extraction fails: it retrieves candidates but
        # no caged arg matches -> extraction returns None -> model fallback.
        env = rt.ask("filter by country Atlantis")
        assert env["results"][0].get("fallback") is True

    def test_menu_generation(self):
        rt = Router(probes=make_probes())
        menu = rt.menu()
        assert len(menu) == 4
        assert all("triggers" in m for m in menu)
        assert all("name" in m for m in menu)

    def test_lint_clean(self):
        rt = Router(probes=make_probes())
        result = rt.lint()
        assert "hard" in result

    def test_pii_mask(self):
        @probe(description="Get user with email",
               triggers=["get user email"],
               args={"uid": int_arg(1, 999)})
        def get_user(uid: int):
            return {"email": "user@example.com", "name": "Test"}

        rt = Router(probes=[get_user], pii_mask=True)
        env = rt.ask("get user email 42")
        blob = json.dumps(env["results"])
        assert "user@example.com" not in blob
        assert "***masked***" in blob

    def test_pii_mask_disabled(self):
        rt = Router(probes=make_probes(), pii_mask=False)
        env = rt.ask("filter country USA")
        assert "USA" in json.dumps(env.get("results", []))


import json  # at the bottom so top-level test functions can use it
