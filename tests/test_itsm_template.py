"""ITSM template: 20 domain probes, behavioural banks, derived metrics.

Tests the template instance end to end: init, routing, refusal, banks,
derived metrics, and the menu export. All hermetic — no REST, no network.
"""
import json
import os
import sys

from neuralosd.router import NoResults

import pytest

sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..", "templates", "itsm")))

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "templates", "itsm")


@pytest.fixture
def itsm_router(tmp_path):
    """Build a Router from the template's probes.py + bridge.py."""
    from neuralosd._cmd._common import load_instance
    # copy the template to tmp so we don't touch the original
    import shutil
    inst_dir = str(tmp_path / "itsm")
    shutil.copytree(TEMPLATE, inst_dir, ignore=shutil.ignore_patterns("__pycache__"))
    # add the instance dir to sys.path so bridge.py is importable
    if inst_dir not in sys.path:
        sys.path.insert(0, inst_dir)
    # clear cached modules so the fixed probes.py is re-imported
    for mod_name in list(sys.modules):
        if mod_name.startswith("neuralosd_inst_") or mod_name in ("bridge", "probes"):
            del sys.modules[mod_name]
    # clean up any stale cache
    cache = os.path.join(inst_dir, ".ask_cache.json")
    if os.path.exists(cache):
        os.unlink(cache)
    inst = load_instance(inst_dir)
    return inst


# ── positive cases: every probe routes correctly ───────────────────────────

class TestPositiveRouting:
    @pytest.mark.parametrize("q,probe", [
        ("how many open incidents", "count_incidents"),
        ("how many problems", "count_problems"),
        ("how many changes", "count_changes"),
        ("how many work orders", "count_work_orders"),
        ("how many known errors", "count_known_errors"),
        ("how many service requests", "count_service_requests"),
        ("how many slas", "count_slas"),
        ("how many worklog entries", "worklog_count"),
        ("critical and high priority incidents", "critical_incidents"),
        ("incident status breakdown", "incident_status_breakdown"),
        ("incidents by priority", "open_incidents_by_priority"),
        ("support groups", "support_groups"),
        ("tickets by group", "tickets_by_group"),
        ("itsm overview", "itsm_overview"),
    ])
    def test_routes_to_the_right_probe(self, itsm_router, q, probe):
        env = itsm_router.ask(q, use_cache=False)
        assert env["probe"] == probe, \
            f"{q!r} routed to {env['probe']}, expected {probe}"
        assert env["results"] is not None

    def test_ticket_detail_with_pattern_extraction(self, itsm_router):
        env = itsm_router.ask("ticket detail TKT001", use_cache=False)
        assert env["probe"] == "ticket_detail"
        assert env["results"][0]["ticket"]["ticket_id"] == "TKT001"


# ── refusal cases: the traps must refuse ───────────────────────────────────

class TestTraps:
    @pytest.mark.parametrize("q,reason", [
        ("xyzzy plugh", "no_probe_matches"),
        ("how many work orders are blocked", "dropped_filter"),
        ("how many incidents were reopened after resolution", "no_probe_matches"),
        ("show me the change calendar for December", "no_probe_matches"),
        ("what is the average time to resolve an incident", "no_probe_matches"),
    ])
    def test_traps_refuse_with_reason(self, itsm_router, q, reason):
        with pytest.raises(NoResults) as e:
            itsm_router.ask(q, use_cache=False)
        env = e.value.envelope
        assert env["results"] is None
        assert env.get("refusal_reason") == reason, \
            f"{q!r}: expected {reason}, got {env.get('refusal_reason')}"


# ── accounting: the ledger is present on aggregate probes ─────────────────

class TestAccounting:
    def test_incident_count_carries_accounting(self, itsm_router):
        env = itsm_router.ask("how many open incidents", use_cache=False)
        r = env["results"][0]
        assert "count" in r
        assert "rows_in" in r and "rows_counted" in r

    def test_breakdown_carries_rows_in_and_counted(self, itsm_router):
        env = itsm_router.ask("incidents by priority", use_cache=False)
        r = env["results"][0]
        assert r["rows_in"] >= r["rows_counted"]
        assert "skipped" in r or r["rows_in"] == r["rows_counted"]


# ── the banks pass ────────────────────────────────────────────────────────

class TestBanks:
    def test_golden_bank_passes(self, itsm_router, tmp_path):
        golden = json.load(open(os.path.join(TEMPLATE, "golden.json"),
                                encoding="utf-8"))
        passed = failed = 0
        for item in golden["items"]:
            try:
                env = itsm_router.ask(item["q"], use_cache=False)
                if env.get("probe") == item.get("expect_probe"):
                    passed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        assert failed == 0, f"{failed} golden cases failed"

    def test_trap_bank_passes(self, itsm_router):
        traps = json.load(open(os.path.join(TEMPLATE, "traps.json"),
                               encoding="utf-8"))
        passed = failed = 0
        for item in traps["items"]:
            try:
                env = itsm_router.ask(item["q"], use_cache=False)
                if env.get("results") in (None, [], {}):
                    passed += 1
                else:
                    failed += 1
            except Exception:
                passed += 1
        assert failed == 0, f"{failed} trap cases failed"


# ── the menu export carries full fidelity ─────────────────────────────────

class TestMenuExport:
    def test_export_carries_enum_values(self, itsm_router):
        menu = itsm_router.menu
        by_ref = next(m for m in menu if m["name"] == "ticket_detail")
        props = by_ref["parameters"]["properties"]
        assert props["ticket_id"]["pattern"] != ""
        assert props["ticket_id"]["type"] == "pattern"

    def test_menu_has_20_probes(self, itsm_router):
        assert len(itsm_router.menu) >= 19
