"""E7 — the gate diverged between builds and could be bypassed by a
coincidence word.

Three fixes, each pinned here:

1. router.json (calibrated floor/margin) was read from the PROCESS CWD
   only, while `calibrate` writes it into the INSTANCE directory. The
   same instance therefore answered differently depending on which
   directory the CLI started in — that is the binary-vs-wheel divergence
   reported on v2.9.10 ("list users in the wallet tier": one build
   refused below_floor with floor 15, the other answered — its floor had
   silently fallen back to the default 3.0). The instance directory is
   now checked first; cwd stays as a legacy fallback.

2. The entity bypass (`_args_seen > 0` disables floor, margin and
   qualifier checks) counted ANY extracted string that appears in the
   question — including values extract_args filled from an OPTIONAL
   arg's default ("all", "today", a default page number). A sentence
   that merely contained that word turned a weak sub-floor match into a
   confident answer. Bypass now requires the question to genuinely name
   the value (re-extraction equality) for optional/defaulted args.

3. The gate decision path is now publishable: NEURALOSD_GATE_TRACE=1
   prints one JSON line to stderr with probe, score, floor, margin,
   args_seen and the decision — identical in the wheel and a frozen
   binary, so build-vs-build divergence is a two-line diff. The last
   trace is also kept on Router.last_gate_trace.
"""
import json
import os

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import Router, _args_seen, _router_config


def _probe(name, triggers, description="d", args=None):
    def fn(**kw):
        return {"probe": name, "ok": True}
    fn.__name__ = name
    return probe_dec(description=description, triggers=triggers, args=args,
                     name=name)(fn)


def _router(tmp_path, probes, **kw):
    return Router(probes=probes, cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"), **kw)


# The v2.9.10 divergence shape: a tier enum OPTIONAL with default "all",
# a weak trigger set, an operator-calibrated floor of 15.
TIER = {"tier": {"type": "enum", "values": ["all", "wallet", "savings"],
                 "required": False, "default": "all"}}
USERS = _probe("users_page", ["list users", "show users", "users"],
               description="Users of the platform", args=TIER)


class TestRouterJsonLivesWithTheInstance:
    def test_calibrated_floor_is_found_via_config_dir(self, tmp_path, monkeypatch):
        inst = tmp_path / "wallet-inst"
        inst.mkdir()
        (inst / "router.json").write_text('{"floor": 15}', encoding="utf-8")
        # the operator's shell is NOWHERE near the instance directory
        monkeypatch.chdir(tmp_path)
        # legacy resolution (no config_dir): floor silently lost — the bug
        assert _router_config("wallet-inst") == {}
        # instance-scoped resolution: honoured
        assert _router_config("wallet-inst", config_dir=str(inst)) == \
            {"floor": 15}

    def test_router_honours_floor_from_any_cwd(self, tmp_path, monkeypatch):
        inst = tmp_path / "wallet-inst"
        inst.mkdir()
        (inst / "router.json").write_text('{"floor": 15}', encoding="utf-8")
        monkeypatch.chdir(tmp_path)          # cwd is the PARENT
        r = _router(tmp_path, [USERS], config_dir=str(inst))
        assert r.floor == 15.0
        # "list users" scores ~12 < 15 → refused, exactly the reported
        # wheel behaviour, now INDEPENDENT of the process cwd
        with pytest.raises(Exception) as e:
            r.ask("list users in the wallet tier", use_cache=False)
        env = e.value.envelope
        assert env.get("refusal_reason") in ("below_floor", "no_probe_matches",
                                             "low_coverage")

    def test_cwd_fallback_still_works(self, tmp_path, monkeypatch):
        # legacy layout: operator runs from a directory holding
        # <name>.router.json — keep it loading
        monkeypatch.chdir(tmp_path)
        (tmp_path / "legacy.router.json").write_text('{"floor": 21}',
                                                     encoding="utf-8")
        assert _router_config("legacy") == {"floor": 21}
        r = Router(probes=[USERS], name="legacy",
                   cache_file=str(tmp_path / "c.json"),
                   audit_file=str(tmp_path / "a.jsonl"))
        assert r.floor == 21.0


class TestGateBypassNeedsANamedNonDefaultValue:
    def test_default_value_in_question_no_longer_bypasses(self, tmp_path):
        # "show all users": tier is filled with the DEFAULT "all"; the word
        # "all" in the question used to count as an entity reference and
        # switched the whole gate off — here even a floor of 99 was beaten
        # into answering. It must be refused instead.
        r = _router(tmp_path, [USERS], floor=99.0)
        with pytest.raises(Exception) as e:
            r.ask("show all users", use_cache=False)
        assert e.value.envelope.get("refusal_reason") == "below_floor"

    def test_named_optional_enum_does_not_bypass_either(self, tmp_path):
        # Even a GENUINELY named optional tier ("wallet") cannot disable the
        # gate: enum extraction is substring-based, so optional values can't
        # be distinguished from default fills. The prose score has to carry
        # an optional-arg question. Conservative by design (E7).
        r = _router(tmp_path, [USERS], floor=99.0)
        with pytest.raises(Exception) as e:
            r.ask("wallet tier users", use_cache=False)
        assert e.value.envelope.get("refusal_reason") == "below_floor"

    def test_required_enum_named_still_bypasses(self, tmp_path):
        # A caged REQUIRED enum hit IS the confidence signal the bypass
        # exists for — the question cannot route at all unless it names a
        # value, and required args only ever hold genuinely extracted ones.
        PENDING = _probe("orders_by_status", ["orders"],
                         description="Orders filtered by status",
                         args={"status": {"type": "enum",
                                          "values": ["pending", "shipped",
                                                     "cancelled"],
                                          "required": True}})
        r = _router(tmp_path, [PENDING], floor=99.0)
        env = r.ask("shipped orders", use_cache=False)
        assert env["probe"] == "orders_by_status"

    def test_args_seen_counts_defaultless_named_args_only(self, tmp_path):
        meta = USERS._probe
        assert _args_seen({"tier": "all"}, "show all users", meta) == 0
        assert _args_seen({"tier": "wallet"}, "wallet tier users", meta) == 0
        assert _args_seen({"tier": "wallet"}, "unrelated", meta) == 0
        # legacy two-arg form (scoring call sites) keeps old behaviour
        assert _args_seen({"tier": "all"}, "show all users") == 1
        REQ = _probe("orders_by_status", ["orders"],
                     args={"status": {"type": "enum",
                                      "values": ["pending", "shipped"],
                                      "required": True}})
        assert _args_seen({"status": "shipped"}, "shipped orders",
                          REQ._probe) == 1

    def test_required_pattern_arg_still_bypasses(self, tmp_path):
        LOOKUP = _probe("incident_lookup", ["status of incident"],
                        args={"ref": {"type": "pattern",
                                      "pattern": r"INC\d+"}})
        r = _router(tmp_path, [LOOKUP], floor=15.0)
        env = r.ask("status of incident INC1234", use_cache=False)
        assert env["probe"] == "incident_lookup"


class TestGateTrace:
    def test_last_gate_trace_records_the_decision(self, tmp_path):
        r = _router(tmp_path, [USERS], floor=99.0)
        with pytest.raises(Exception):
            r.ask("show all users", use_cache=False)
        t = r.last_gate_trace
        assert t["probe"] == "users_page"
        assert t["floor"] == 99.0
        assert t["args_seen"] == 0        # the E7 fix, visible in the trace
        assert t["decision"] == "below_floor"
        assert "score" in t and "margin" in t

    def test_gate_trace_stderr_publication(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("NEURALOSD_GATE_TRACE", "1")
        r = _router(tmp_path, [USERS], floor=99.0)
        with pytest.raises(Exception):
            r.ask("show all users", use_cache=False)
        err = capsys.readouterr().err
        assert "gate_trace:" in err
        line = [l for l in err.splitlines() if l.startswith("gate_trace:")][-1]
        t = json.loads(line.split("gate_trace:", 1)[1])
        assert t["decision"] == "below_floor" and t["probe"] == "users_page"

    def test_no_trace_noise_by_default(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("NEURALOSD_GATE_TRACE", raising=False)
        PENDING = _probe("orders_by_status", ["orders"],
                         args={"status": {"type": "enum",
                                          "values": ["pending", "shipped"],
                                          "required": True}})
        r = _router(tmp_path, [PENDING], floor=99.0)
        env = r.ask("shipped orders", use_cache=False)
        assert env["probe"] == "orders_by_status"
        assert "gate_trace:" not in capsys.readouterr().err
