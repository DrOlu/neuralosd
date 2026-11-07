"""Mesh protocol hardening — preflight, provenance, trust, verification.

Every test runs against stubs. No NATS, no gateway, no network in CI.

What these tests pin, feature by feature:

  PREFLIGHT   dry_run agreement with ask(); a preflight never executes a
              probe, touches the cache, or writes the audit log
  REGISTER    the versioned contract blob is network-free and publishable
  PROVENANCE  menu/gate/protocol versions + answer hash + signature on
              every verdict; redaction happens BEFORE signing
  TRUST       the four classes decide what crosses the boundary, applied
              uniformly — no per-caller special cases
  DISCOVERY   the same scorer that routes local questions; published
              triggers beat bare skill names; self excluded; preflight
              filters peers that would only refuse
  DELEGATION  refusals travel as refusals; loops and hop budgets cut; the
              deadline cuts before the call; transport errors carry a
              bare error code; peer answers verified against what the
              peer advertised
  GOSSIP      proposals computed locally with honest verdicts; menus
              reconcile; only the owner can explain its own fingerprints
  TRANSPORT   retry only when provably idempotent; error codes are bare
              tokens; deadlines bound the socket timeout
"""
import json
import os
import socket
import urllib.error

import pytest

from neuralosd import loop
from neuralosd import synapse as S
from neuralosd.probe import probe as probe_dec
from neuralosd.router import GATE_VERSION, NoResults, Router

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "refusal_gate.json")


# ── shared builders ────────────────────────────────────────────────────────

def _probe(name, triggers, description="d", args=None, boom=False):
    def fn(**kw):
        if boom:
            raise RuntimeError("probe exploded on purpose")
        return {"probe": name, "ok": True}

    fn.__name__ = name
    return probe_dec(description=description, triggers=triggers,
                     args=args, name=name)(fn)


def _router(tmp_path, probes, **kw):
    return Router(probes=probes, cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"), **kw)


def _agents(*agents):
    return [a for a in agents if a is not None]


WEMA = {"agent_id": "wema-bmc",
        "capabilities": [{"name": "open_incidents",
                          "description": "Count of open incidents.",
                          "triggers": ["how many incidents",
                                       "incident count"]}],
        "skills": ["open_incidents"]}
CHINOOK = {"agent_id": "chinook",
           "capabilities": [{"name": "by_genre",
                             "description": "Tracks by genre.",
                             "triggers": ["tracks by genre"]}]}


class StubTransport:
    """The phonebook + the answers, scripted. Records everything."""

    def __init__(self, agents=None, responses=None, prefights=None,
                 fail_preflight=False):
        self.agents = agents or []
        self.responses = responses or {}
        self.preflights = prefights or {}
        self.fail_preflight = fail_preflight
        self.calls = []

    def discover(self, skill=""):
        self.calls.append(("discover", skill))
        return self.agents

    def request(self, agent_id, text, timeout=None):
        self.calls.append(("request", agent_id, text, timeout))
        return self.responses.get(agent_id, {"status": "no_response"})

    def preflight(self, target, text, deadline=None):
        self.calls.append(("preflight", target, text))
        if self.fail_preflight:
            raise S.SynapseError(f"no preflight at {target}", code="not_found")
        return self.preflights.get(target, {
            "would": "refused", "refusal_reason": "no_probe_matches",
            "menu_version": "m1", "gate_version": 1})

    def heartbeat(self, agent_id, beat=None, deadline=None):
        self.calls.append(("heartbeat", agent_id))
        return {"ok": True}

    def register(self, blob):
        self.calls.append(("register", blob.get("agent_id")))
        return {"ok": True}


def _menu_router(tmp_path, boom=False):
    """Two probes: incidents (answers) and genre (enum arg)."""
    return _router(tmp_path, [
        _probe("count_incidents", ["how many incidents"],
               "Count incidents", boom=boom),
        _probe("by_genre", ["tracks by genre"], "Tracks by genre",
               args={"genre": {"type": "enum", "values": ["Rock", "Jazz"]}}),
    ])


def _agent(tmp_path, transport=None, **kw):
    return S.MeshAgent(instance_name="bxclinic",
                       router=_menu_router(tmp_path),
                       transport=transport or StubTransport(), **kw)


# ── the versioned contract ─────────────────────────────────────────────────

class TestContract:
    def test_gate_version_is_fixture_gate_version(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            spec = json.load(fh)
        assert spec["gate_version"] == GATE_VERSION

    def test_registration_blob_is_versioned(self, tmp_path):
        a = _agent(tmp_path)
        reg = a.register()
        assert reg["agent_id"] == "bxclinic"
        assert reg["mesh_protocol_version"] == S.MESH_PROTOCOL_VERSION
        assert reg["gate_version"] == GATE_VERSION
        assert reg["menu_version"] == a.router.menu_version
        assert reg["probes"] == len(a.router.metas)
        assert reg["heartbeat_seconds"] == S.HEARTBEAT_SECONDS
        assert reg["door"].startswith("neuralosd ask")

    def test_register_is_network_free(self, tmp_path):
        t = StubTransport()
        a = _agent(tmp_path, t)
        a.register()
        assert t.calls == [], "register() must not touch the transport"

    def test_capabilities_publish_triggers_and_arg_types_only(self, tmp_path):
        """Enum VALUE lists stay local: a stranger who can read the values
        can read the rows; a stranger who can read types can only ask."""
        a = _agent(tmp_path)
        caps = a.capabilities()
        by_name = {c["name"]: c for c in caps}
        assert by_name["count_incidents"]["triggers"] == ["how many incidents"]
        assert by_name["by_genre"]["args"]["genre"]["type"] == "enum"
        assert "values" not in by_name["by_genre"]["args"]["genre"]

    def test_digest_has_no_vocabulary(self, tmp_path):
        a = _agent(tmp_path)
        blob = json.dumps(a.digest())
        assert "how many incidents" not in blob
        assert "tracks by genre" not in blob

    def test_menu_gossip_diff_reconciles(self):
        own = [{"name": "a", "triggers": ["x"]},
               {"name": "b", "triggers": ["y"]}]
        peer = [{"name": "a", "triggers": ["x", "z"]},
                {"name": "c", "triggers": ["w"]}]
        d = S.diff_menus(own, peer, "me", "peer")
        assert d["shared"] == ["a"]
        assert d["only_local"] == ["b"]
        assert d["only_peer"] == ["c"]
        assert len(d["drifted"]) == 1
        drift = d["drifted"][0]
        assert drift["added_peer_side"] == ["z"]
        assert drift["added_locally"] == []


# ── preflight: would-you-answer, for free ──────────────────────────────────

class TestPreflight:
    def test_preflight_agrees_with_handle(self, tmp_path):
        a = _agent(tmp_path)
        for q in ("how many incidents", "what is the weather in lagos"):
            pf = a.preflight(q)
            h = a.handle(q, trust="trusted")
            assert pf["would_answer"] == (h["status"] == "answered"), q
            assert pf["probe"] == h.get("probe"), q

    def test_preflight_never_executes_a_probe(self, tmp_path):
        calls = []

        def fn(**kw):
            calls.append(kw)
            return {"probe": "count_incidents", "ok": True}

        fn.__name__ = "count_incidents"
        p = probe_dec(description="Count incidents",
                      triggers=["how many incidents"],
                      name="count_incidents")(fn)
        router = _router(tmp_path, [p])
        a = S.MeshAgent(instance_name="x", router=router,
                        transport=StubTransport())
        pf = a.preflight("how many incidents")
        assert calls == [] and pf["would"] == "answered"

    def test_preflight_is_side_effect_free(self, tmp_path):
        router = _menu_router(tmp_path)
        a = S.MeshAgent(instance_name="x", router=router,
                        transport=StubTransport())
        for _ in range(50):
            a.preflight("how many incidents")
        assert not os.path.exists(str(tmp_path / "a.jsonl"))
        assert not os.path.exists(str(tmp_path / "c.json"))
        # ask still works normally afterwards
        assert router.ask("how many incidents")["probe"] == "count_incidents"

    def test_preflight_carries_versions_and_signature(self, tmp_path):
        a = _agent(tmp_path, signing_key="mesh-secret")
        pf = a.preflight("how many incidents")
        assert pf["menu_version"] == a.router.menu_version
        assert pf["gate_version"] == GATE_VERSION
        assert pf["mesh_protocol_version"] == S.MESH_PROTOCOL_VERSION
        assert pf["signature"]["alg"] == "hmac-sha256"
        ok, why = S.verify_signature(pf, "mesh-secret")
        assert ok and why == "signature_ok"


# ── provenance: versions, hash, signature ─────────────────────────────────

class TestProvenance:
    def test_handle_answer_carries_versions(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle("how many incidents")
        assert h["status"] == "answered"
        assert h["probe"] == "count_incidents"
        assert h["menu_version"] == a.router.menu_version
        assert h["gate_version"] == GATE_VERSION
        assert h["mesh_protocol_version"] == S.MESH_PROTOCOL_VERSION

    def test_handle_refusal_shape(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle("what is the weather in lagos tomorrow")
        assert h["status"] == "refused"
        assert h["refusal_reason"] == "no_probe_matches"
        assert isinstance(h.get("error"), str) and h["error"]

    def test_probe_error_is_reported_not_swallowed(self, tmp_path):
        router = _router(tmp_path, [
            _probe("count_incidents", ["how many incidents"], boom=True)])
        a = S.MeshAgent(instance_name="x", router=router,
                        transport=StubTransport())
        h = a.handle("how many incidents")
        assert h["status"] == "probe_error"
        assert h["error_code"] == "probe_error"
        assert "exploded" in h["error"]

    def test_protocol_mismatch_is_refused_not_downgraded(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle("how many incidents", mesh_protocol_version=2)
        assert h["status"] == "incompatible"
        assert h["error_code"] == "mesh_protocol_mismatch"

    def test_bad_question_type(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle(None)
        assert h["status"] == "error"
        assert h["error_code"] == "bad_question"

    def test_answer_hash_deterministic_and_ts_insensitive(self, tmp_path):
        a = _agent(tmp_path)
        h1 = a.handle("how many incidents")
        h2 = a.handle("how many incidents")
        assert S.answer_hash(h1) == S.answer_hash(h2)
        h3 = json.loads(json.dumps(h1))
        h3["ts"] = h1["ts"] + 999
        assert S.answer_hash(h3) == S.answer_hash(h1)
        h3["results"] = [{"tampered": True}]
        assert S.answer_hash(h3) != S.answer_hash(h1)

    def test_signed_answer_verifies_and_tamper_fails(self, tmp_path):
        a = _agent(tmp_path, signing_key="mesh-secret")
        h = a.handle("how many incidents")
        ok, why = S.verify_signature(h, "mesh-secret")
        assert ok and why == "signature_ok"
        assert h["signature"]["authenticated"] is True
        assert len(h["signature"]["kid"]) == 8
        tampered = json.loads(json.dumps(h))
        tampered["results"] = [{"probe": "evil"}]
        ok, why = S.verify_signature(tampered, "mesh-secret")
        assert not ok and why == "tampered"
        ok, why = S.verify_signature(h, "wrong-key")
        assert not ok and why == "bad_signature"

    def test_unsigned_answer_is_integrity_only(self, tmp_path):
        a = _agent(tmp_path)          # no NEURALOSD_MESH_KEY
        h = a.handle("how many incidents")
        assert h["signature"]["authenticated"] is False
        ok, why = S.verify_signature(h)
        assert ok and why == "integrity_only"

    def test_unsigned_body_is_unsigned(self, tmp_path):
        ok, why = S.verify_signature({"no": "signature"})
        assert not ok and why == "unsigned"


# ── trust classes: what crosses the boundary ───────────────────────────────

class TestTrust:
    def test_level_matrix(self):
        want = {"self": (True, True, True, True),
                "trusted": (True, True, True, True),
                "known": (False, False, False, True),
                "untrusted": (False, False, False, False)}
        for level, (scores, cands, disc, deleg) in want.items():
            p = S.TrustPolicy.for_level(level)
            assert (p.expose_scores, p.expose_candidates,
                    p.expose_discarded, p.allow_delegation) == \
                (scores, cands, disc, deleg), level

    def test_unknown_level_is_untrusted(self):
        assert S.TrustPolicy.for_level("root").level == "untrusted"

    def test_known_gets_band_not_scores(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle("how many incidents", trust="known")
        assert "scores" not in h
        assert h["confidence_band"] in ("low", "medium", "high")

    def test_trusted_gets_the_full_ledger(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle("how many incidents", trust="trusted")
        assert h.get("scores")
        assert "confidence_band" not in h

    def test_untrusted_cannot_delegate_through_us(self, tmp_path):
        a = _agent(tmp_path)
        h = a.handle("how many incidents", trust="untrusted")
        assert h["delegation_allowed"] is False

    def test_redaction_happens_before_signing(self, tmp_path):
        """The signature must match what was SENT: verify the signed body
        AFTER removing everything a known caller shouldn't see."""
        a = _agent(tmp_path, signing_key="mesh-secret")
        h = a.handle("how many incidents", trust="known")
        redacted = {k: v for k, v in h.items()
                    if k not in ("scores", "discarded", "signature")}
        assert S.answer_hash(h) == S.answer_hash({**h})
        # the signature covers the redacted view, not the full ledger
        sub = {k: v for k, v in h.items() if k not in S._HASH_EXCLUDE}
        assert "scores" not in sub
        ok, _ = S.verify_signature(h, "mesh-secret")
        assert ok

    def test_withheld_ledger_is_labelled(self, tmp_path):
        """Redaction must look like redaction, not absence."""
        a = _agent(tmp_path)
        body = {"status": "answered", "scores": [{"probe": "p", "score": 9}],
                "discarded": [{"dropped": "since yesterday"}]}
        out = a._finish(json.loads(json.dumps(body)),
                        S.TrustPolicy.for_level("known"))
        assert out.get("discarded") is None
        assert out["ledger"] == "withheld"

    def test_confidence_band_boundaries(self):
        assert S.confidence_band(None, 3.0) is None
        assert S.confidence_band(0, 3.0) is None
        assert S.confidence_band(6.0, 3.0) == "high"      # >= 2x floor
        assert S.confidence_band(3.0, 3.0) == "medium"    # >= floor
        assert S.confidence_band(2.9, 3.0) == "low"


# ── discovery: the router's own scorer, pointed outward ────────────────────

class TestDiscovery:
    def test_published_triggers_rank_peers(self, tmp_path):
        t = StubTransport(agents=_agents(WEMA, CHINOOK))
        peers = S.discover_peers(t, "how many incidents were reopened",
                                 "bxclinic")
        assert peers and peers[0]["agent_id"] == "wema-bmc"
        assert "incident" in peers[0]["matched"]
        assert peers[0]["probe"] == "open_incidents"

    def test_bare_skill_names_still_match(self, tmp_path):
        t = StubTransport(agents=[{"agent_id": "wema-bmc",
                                   "skills": ["critical_incidents"]}])
        peers = S.discover_peers(t, "how many critical incidents", "me")
        assert peers and peers[0]["probe"] == "critical_incidents"

    def test_generic_only_question_discovers_nothing(self, tmp_path):
        t = StubTransport(agents=_agents(WEMA))
        assert S.discover_peers(t, "how many", "me") == []

    def test_self_is_excluded(self, tmp_path):
        t = StubTransport(agents=_agents(WEMA))
        assert S.discover_peers(t, "how many incidents",
                                "wema-bmc") == []

    def test_limit_caps_results(self, tmp_path):
        agents = [{"agent_id": f"a{i}",
                   "skills": ["incident counting"]} for i in range(10)]
        t = StubTransport(agents=agents)
        peers = S.discover_peers(t, "incident counting results", "me",
                                 limit=3)
        assert len(peers) == 3

    def test_malformed_agents_are_skipped(self, tmp_path):
        t = StubTransport(agents=_agents(
            "not-a-dict", 42, None,
            {"no_id": 1},
            {"agent_id": "", "skills": ["incident"]},
            {"agent_id": "ok", "capabilities": ["not-a-dict", None,
             {"description": "no name"}]},
            WEMA))
        peers = S.discover_peers(t, "how many incidents", "me")
        assert [p["agent_id"] for p in peers] == ["wema-bmc"]

    def test_preflight_discovery_keeps_only_willing_peers(self, tmp_path):
        t = StubTransport(agents=_agents(WEMA), prefights={
            "wema-bmc": {"would": "answered", "probe": "open_incidents",
                         "menu_version": "m1", "gate_version": 1}})
        peers = S.discover_peers(t, "how many incidents", "me",
                                 preflight=True)
        assert peers and peers[0]["would"] == "answered"
        assert peers[0]["probe"] == "open_incidents"

    def test_preflight_discovery_drops_refusing_peers(self, tmp_path):
        t = StubTransport(agents=_agents(WEMA))   # default preflight: refused
        assert S.discover_peers(t, "how many incidents", "me",
                                preflight=True) == []

    def test_preflight_not_found_degrades_to_unverified(self, tmp_path):
        t = StubTransport(agents=_agents(WEMA), fail_preflight=True)
        peers = S.discover_peers(t, "how many incidents", "me",
                                 preflight=True)
        assert peers and peers[0]["would"] is None

    def test_preflight_never_executes_local_probes(self, tmp_path):
        calls = []

        def fn(**kw):
            calls.append(kw)
            return {"probe": "count_incidents", "ok": True}

        fn.__name__ = "count_incidents"
        p = probe_dec(description="c", triggers=["how many incidents"],
                      name="count_incidents")(fn)
        a = S.MeshAgent(instance_name="me",
                        router=_router(tmp_path, [p]),
                        transport=StubTransport(agents=_agents(WEMA)))
        S.discover_peers(a.transport, "how many incidents", "me")
        assert calls == []


# ── delegation: honest about refusals, safe about loops ────────────────────

class TestDelegation:
    def test_legacy_compat_raw_response_kept(self, tmp_path):
        t = StubTransport(responses={"p": {"answer": 42, "status": "ok"}})
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q")
        assert d["source"] == "synapse_peer"
        assert d["peer"] == "p"
        assert d["response"]["answer"] == 42
        assert d["status"] == "answered_unverified"
        assert d["verified"] is False

    def test_missing_agent_id_returns_none(self, tmp_path):
        t = StubTransport()
        assert S.delegate_to_peer(t, {}, "q") is None
        assert S.delegate_to_peer(t, "not-a-dict", "q") is None

    def test_refusal_travels_as_a_refusal(self, tmp_path):
        t = StubTransport(responses={
            "p": {"status": "refused", "refusal_reason": "no_probe_matches",
                  "error": "no probe matched"}})
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q")
        assert d["status"] == "refused"
        assert d["refusal_reason"] == "no_probe_matches"
        assert d["verified"] is False

    def test_verified_answer_against_advertised_capabilities(self, tmp_path):
        t = StubTransport(responses={
            "p": {"status": "answered", "probe": "open_incidents",
                  "results": [{"count": 7}], "menu_version": "m1",
                  "gate_version": 1,
                  "mesh_protocol_version": S.MESH_PROTOCOL_VERSION}})
        caps = [{"name": "open_incidents"}]
        d = S.delegate_to_peer(t, {"agent_id": "p", "capabilities": caps},
                               "q")
        assert d["status"] == "answered"
        assert d["verified"] is True
        assert d["verify_reason"] == "ok"

    def test_unknown_probe_fails_verification(self, tmp_path):
        t = StubTransport(responses={
            "p": {"status": "answered", "probe": "does_not_exist",
                  "results": [{"x": 1}]}})
        d = S.delegate_to_peer(t, {"agent_id": "p",
                                   "capabilities": [{"name": "real"}]}, "q")
        assert d["verified"] is False
        assert "unknown_probe" in d["verify_reason"]

    def test_loop_detected_before_any_call(self, tmp_path):
        t = StubTransport(responses={"p": {"status": "answered"}})
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q", visited=["p"])
        assert d["status"] == "loop_detected"
        assert ("request", "p") not in [(c[0], c[1]) for c in t.calls]

    def test_hop_budget_exhausted(self, tmp_path):
        t = StubTransport()
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q",
                               visited=["a", "b", "c"], max_hops=3)
        assert d["status"] == "hop_limit"
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q",
                               visited=["a", "b"], max_hops=3)
        assert d["status"] != "hop_limit"

    def test_deadline_cut_before_the_call(self, tmp_path):
        t = StubTransport()
        dl = S.Deadline.from_ms(1)
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q", deadline=dl)
        assert d["status"] == "deadline_exceeded"
        assert t.calls == []

    def test_live_deadline_bounds_the_socket_timeout(self, tmp_path):
        t = StubTransport(responses={"p": {"status": "answered"}})
        dl = S.Deadline.from_ms(5000)
        S.delegate_to_peer(t, {"agent_id": "p"}, "q", deadline=dl)
        req = [c for c in t.calls if c[0] == "request"][0]
        assert req[3] is not None and 0 < req[3] <= 5.0

    def test_transport_failure_carries_error_code(self, tmp_path):
        class Boom(StubTransport):
            def request(self, *a, **k):
                raise S.SynapseError("gateway down", code="unreachable")
        d = S.delegate_to_peer(Boom(), {"agent_id": "p"}, "q")
        assert d["status"] == "delegation_failed"
        assert d["error_code"] == "unreachable"

    def test_trust_denies_delegation_before_the_call(self, tmp_path):
        t = StubTransport()
        d = S.delegate_to_peer(t, {"agent_id": "p"}, "q", trust="untrusted")
        assert d["status"] == "delegation_denied"
        assert t.calls == []

    def test_visited_travels_for_the_next_hop(self, tmp_path):
        t = StubTransport(responses={"b": {"status": "no_response"}})
        d = S.delegate_to_peer(t, {"agent_id": "b"}, "q", visited=["a"])
        assert d["visited"] == ["a", "b"]
        assert d["hop"] == 2


class TestPeerVerification:
    def test_refused_is_a_verdict_not_a_failure_to_verify(self):
        v = S.verify_peer_answer({"status": "refused",
                                  "refusal_reason": "low_coverage"})
        assert v["valid"] is False and v["reasons"] == ["peer_refused"]

    def test_missing_status(self):
        v = S.verify_peer_answer({"probe": "x", "results": [1]})
        assert "missing_status" in v["reasons"]

    def test_empty_results(self):
        v = S.verify_peer_answer({"status": "answered", "probe": "x",
                                  "results": []})
        assert "empty_results" in v["reasons"]

    def test_missing_probe(self):
        v = S.verify_peer_answer({"status": "answered", "results": [1]})
        assert "missing_probe" in v["reasons"]

    def test_stale_menu_and_gate_mismatch(self):
        v = S.verify_peer_answer(
            {"status": "answered", "probe": "x", "results": [1],
             "menu_version": "old", "gate_version": 0},
            expected_menu_version="new", expected_gate_version=1)
        assert "stale_menu" in v["reasons"]
        assert "gate_version_mismatch" in v["reasons"]

    def test_unsupported_protocol_flagged(self):
        v = S.verify_peer_answer({"status": "answered", "probe": "x",
                                  "results": [1],
                                  "mesh_protocol_version": 99})
        assert "protocol_unsupported" in v["reasons"]

    def test_non_dict(self):
        v = S.verify_peer_answer("hello")
        assert v["valid"] is False and v["reasons"] == ["not_a_dict"]


# ── heartbeats: liveness + drift ───────────────────────────────────────────

class TestHeartbeat:
    def test_drift_detected_against_previous(self, tmp_path):
        a = _agent(tmp_path)
        beat = a.heartbeat({"menu_version": "old-menu"})
        assert beat["changed"] is True
        assert beat["previous_menu_version"] == "old-menu"
        same = a.heartbeat({"menu_version": beat["menu_version"]})
        assert same["changed"] is False

    def test_heartbeat_loop_bounded_and_annotated(self, tmp_path):
        t = StubTransport()
        a = _agent(tmp_path, t)
        beats = a.heartbeat_loop(interval=0, max_beats=3, publish=True)
        assert len(beats) == 3
        assert [b["beat"] for b in beats] == [1, 2, 3]
        assert all(b["published"] is True for b in beats)

    def test_heartbeat_publish_failure_is_annotated(self, tmp_path):
        class Down(StubTransport):
            def heartbeat(self, *a, **k):
                raise S.SynapseError("down", code="unreachable")
        a = _agent(tmp_path, Down())
        beats = a.heartbeat_loop(interval=0, max_beats=1, publish=True)
        assert beats[0]["published"] is False
        assert beats[0]["publish_error"] == "unreachable"

    def test_transport_without_heartbeat_verb(self, tmp_path):
        class Old(StubTransport):
            heartbeat = None  # type: ignore[assignment]
        a = _agent(tmp_path, Old())
        beats = a.heartbeat_loop(interval=0, max_beats=1, publish=True)
        assert beats[0]["publish_error"] == "unsupported"


# ── gossip: proposals, never auto-application ──────────────────────────────

class TestGossip:
    def _audit(self, tmp_path, refused):
        path = str(tmp_path / "ask_audit.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for q in refused:
                fh.write(json.dumps({"mode": "refused",
                                     "refusal_reason": "no_probe_matches",
                                     "question": q}) + "\n")
            fh.write(json.dumps({"mode": "answered",
                                 "question": "how many incidents"}) + "\n")
            fh.write("not json at all\n")
        return path

    def test_refusals_from_audit_filters_and_parses(self, tmp_path):
        path = self._audit(tmp_path, ["how many incident attachments exist",
                                      "how many incident attachments",
                                      "count incident attachments"])
        out = S.refusals_from_audit(path)
        assert len(out) == 3
        assert all("incident" in q for q in out)

    def test_refusals_from_missing_file(self, tmp_path):
        assert S.refusals_from_audit(str(tmp_path / "nope.jsonl")) == []

    def test_proposal_extend_trigger(self, tmp_path):
        """A phrasing variant: every core token is known somewhere in the
        menu, so extending the nearest probe's triggers is plausible."""
        r = _menu_router(tmp_path)
        props = S.propose_triggers(r, [
            "number of incidents", "total incidents count",
            "incidents count"], min_count=2)
        assert props and props[0]["probe"] == "count_incidents"
        assert props[0]["verdict"] == "extend_trigger"
        assert props[0]["already_present"] is False
        assert props[0]["evidence"] >= 2

    def test_proposal_new_subject_needs_a_probe_not_a_trigger(self, tmp_path):
        """'attachment' is known to NO probe: a trigger on count_incidents
        would teach the router to answer a question the probe cannot."""
        r = _menu_router(tmp_path)
        props = S.propose_triggers(r, [
            "how many incident attachments exist",
            "how many incident attachments",
            "count incident attachments"], min_count=2)
        assert props and props[0]["verdict"] == "new_subject"
        assert "attachment" in props[0]["unknown_tokens"]

    def test_proposal_covered(self, tmp_path):
        r = _menu_router(tmp_path)
        props = S.propose_triggers(r, ["how many incidents"] * 3,
                                   min_count=2)
        assert props and props[0]["verdict"] == "covered"
        assert props[0]["already_present"] is True

    def test_proposal_new_probe_when_nothing_is_near(self, tmp_path):
        r = _menu_router(tmp_path)
        props = S.propose_triggers(r, [
            "quarterly revenue by geography", "revenue by geography q",
            "geography revenue totals"], min_count=2)
        assert props and props[0]["verdict"] == "new_probe"
        assert props[0]["probe"] is None

    def test_min_count_and_max_proposals(self, tmp_path):
        r = _menu_router(tmp_path)
        assert S.propose_triggers(r, ["how many incidents"],
                                  min_count=2) == []
        many = ["number of incidents", "incidents count",
                "total incidents count", "incidents total count"]
        props = S.propose_triggers(r, many, min_count=2, max_proposals=1)
        assert len(props) == 1

    def test_gossip_blob_is_hash_only(self, tmp_path):
        r = _menu_router(tmp_path)
        path = self._audit(tmp_path, ["how many incident attachments exist",
                                      "how many incident attachments"])
        a = S.MeshAgent(instance_name="me", router=r,
                        transport=StubTransport())
        blob = a.gossip_blob(audit_path=path)
        assert blob["origin"] == "me"
        text = json.dumps(blob["fingerprints"])
        assert "attachment" not in text and "incident" not in text

    def test_explain_fingerprints_owner_only_round_trip(self, tmp_path):
        gaps = ["how many incident attachments exist",
                "how many incident attachments",
                "count incident attachments"]
        cl = loop.cluster_gaps([{"question": g} for g in gaps])
        blob = loop.export_fingerprints(cl)
        mine = loop.explain_fingerprints(blob["fingerprints"], gaps)
        assert mine and all(e["known"] for e in mine)
        assert all(e["tokens"] and not any(q in e["tokens"] for q in gaps)
                   for e in mine)
        foreign = loop.explain_fingerprints(blob["fingerprints"],
                                            ["unrelated other questions here",
                                             "unrelated other questions"])
        assert all(e["known"] is False and e["tokens"] == []
                   for e in foreign)


# ── transport: retry what is safe, fail fast on what is not ────────────────

class TestTransport:
    def _client(self):
        return S.SynapseHTTP(gateway="http://gw", timeout=5.0, token="t",
                             retries=2)

    def test_get_retries_on_transient_and_recovers(self, monkeypatch):
        c = self._client()
        calls = {"n": 0}

        def flaky(url, headers=None, data=None, timeout=5.0, method=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise S.SynapseError("flap", code="timeout")
            return json.dumps({"ok": True}).encode()

        monkeypatch.setattr(S, "_http", flaky)
        assert c._get("/api/mesh/health") == {"ok": True}
        assert calls["n"] == 3

    def test_get_exhausts_retries_then_raises(self, monkeypatch):
        c = self._client()

        def always(url, **kw):
            raise S.SynapseError("down", code="unreachable")
        monkeypatch.setattr(S, "_http", always)
        with pytest.raises(S.SynapseError) as e:
            c._get("/api/mesh/status")
        assert e.value.code == "unreachable"

    def test_non_idempotent_post_never_retries(self, monkeypatch):
        c = self._client()
        calls = {"n": 0}

        def always(url, headers=None, data=None, timeout=5.0, method=None):
            calls["n"] += 1
            raise S.SynapseError("flap", code="gateway_error")
        monkeypatch.setattr(S, "_http", always)
        with pytest.raises(S.SynapseError):
            c._post("/api/mesh/dispatch", {"target": "x", "input": {}})
        assert calls["n"] == 1

    def test_post_with_idempotency_key_retries(self, monkeypatch):
        c = self._client()
        calls = {"n": 0}

        def flaky(url, headers=None, data=None, timeout=5.0, method=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise S.SynapseError("flap", code="rate_limited")
            return json.dumps({"ok": True}).encode()
        monkeypatch.setattr(S, "_http", flaky)
        assert c._post("/api/mesh/preflight", {"target": "x"},
                       idempotency_key="pf-1") == {"ok": True}
        assert calls["n"] == 2

    def test_idempotency_key_header_sent(self, monkeypatch):
        c = self._client()
        seen = {}

        def capture(url, headers=None, data=None, timeout=5.0, method=None):
            seen.update(headers or {})
            return json.dumps({}).encode()
        monkeypatch.setattr(S, "_http", capture)
        c._post("/api/mesh/tasks", {"target": "x", "taskId": "abc-123"})
        assert seen.get("Idempotency-Key") == "abc-123"

    def test_authorization_header(self, monkeypatch):
        c = self._client()
        seen = {}

        def capture(url, headers=None, data=None, timeout=5.0, method=None):
            seen.update(headers or {})
            return json.dumps({}).encode()
        monkeypatch.setattr(S, "_http", capture)
        c._get("/api/mesh/agents")
        assert seen.get("Authorization") == "Bearer t"

    def test_error_mapping(self):
        assert S._error_code(401) == "unauthorized"
        assert S._error_code(403) == "unauthorized"
        assert S._error_code(404) == "not_found"
        assert S._error_code(409) == "conflict"
        assert S._error_code(429) == "rate_limited"
        assert S._error_code(502) == "gateway_error"
        assert S._error_code(418) == "mesh_error"
        assert S._error_code(None) == "mesh_error"

    def test_http_error_maps_to_code(self, monkeypatch):
        c = self._client()

        def raise_http(url, headers=None, data=None, timeout=5.0,
                       method=None):
            raise urllib.error.HTTPError(url, 404, "nope", hdrs=None,
                                         fp=None)
        monkeypatch.setattr(S.urllib.request, "urlopen", raise_http)
        with pytest.raises(S.SynapseError) as e:
            c._get("/api/mesh/agents/x")
        assert e.value.code == "not_found" and e.value.status == 404

    def test_http_error_with_unreadable_body_still_maps(self, monkeypatch):
        c = self._client()

        class DeadError(urllib.error.HTTPError):
            def read(self):
                raise RuntimeError("socket closed")

        def raise_http(url, headers=None, data=None, timeout=5.0,
                       method=None):
            raise DeadError(url, 502, "bad gateway", hdrs=None, fp=None)
        monkeypatch.setattr(S.urllib.request, "urlopen", raise_http)
        with pytest.raises(S.SynapseError) as e:
            c._get("/api/mesh/agents/x")
        assert e.value.code == "gateway_error" and e.value.status == 502

    def test_url_error_maps_unreachable(self, monkeypatch):
        c = self._client()

        def raise_url(url, headers=None, data=None, timeout=5.0,
                      method=None):
            raise urllib.error.URLError(socket.gaierror(8, "nodename"))
        monkeypatch.setattr(S.urllib.request, "urlopen", raise_url)
        with pytest.raises(S.SynapseError) as e:
            c._get("/x")
        assert e.value.code == "unreachable"

    def test_token_from_env_wins_over_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("REACTORPRO_GATEWAY_TOKEN", "env-tok")
        f = tmp_path / "gateway.env"
        f.write_text("LIVEAGENT_GATEWAY_TOKEN=file-tok\n", encoding="utf-8")
        c = S.SynapseHTTP(token_path=str(f))
        assert c.token == "env-tok"

    def test_token_from_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REACTORPRO_GATEWAY_TOKEN", raising=False)
        f = tmp_path / "gateway.env"
        f.write_text('LIVEAGENT_GATEWAY_TOKEN="file-tok"\n',
                     encoding="utf-8")
        c = S.SynapseHTTP(token_path=str(f))
        assert c.token == "file-tok"

    def test_deadline_semantics(self):
        dl = S.Deadline.from_ms(50)
        assert not dl.expired()
        assert dl.remaining_ms() <= 50
        dl = S.Deadline.from_ms(-5)
        assert dl.expired() and dl.remaining_ms() == 0
        assert S.Deadline.from_ms(None).budget_ms == S.DEFAULT_DEADLINE_MS

    def test_reply_unwrap_payload_text(self):
        out = S._read_reply({"payload": {"text": "hi"}, "from": "p"})
        assert out["text"] == "hi" and out["from"] == "p"

    def test_reply_unwrap_reactorpro_output(self):
        out = S._read_reply({"payload": {"output": {"result": 42}}})
        assert out["text"] == "42"

    def test_reply_error_envelope_raises(self):
        with pytest.raises(S.SynapseError):
            S._read_reply({"error": {"code": "E", "message": "bad"}})

    def test_reply_unrecognised_shape(self):
        with pytest.raises(S.SynapseError):
            S._read_reply("a string")
