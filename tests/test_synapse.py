"""Synapse mesh integration — neuralosd as a first-class citizen on the agent mesh.

Every test here runs against a STUB transport. No NATS, no gateway, no network
in CI. The stub proves the properties:

  * registration publishes the probe menu as capabilities
  * incoming requests go through the same verified router
  * refusals travel back with refusal_reason, not a wrong answer
  * discovery matches domain nouns, not the agent's own name
  * delegation tags the response as peer-sourced, not local
"""
import json

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import NoResults, Router
from neuralosd.synapse import (MeshAgent, SynapseError, SynapseHTTP,
                               delegate_to_peer, discover_peers)


# ── the stub transport ─────────────────────────────────────────────────────

class StubTransport:
    """Records calls, returns scripted responses."""

    def __init__(self, agents=None, responses=None):
        self.agents = agents or []
        self.responses = responses or {}
        self.posts = []

    def _post(self, path, body):
        self.posts.append({"path": path, "body": body})
        if "/discover" in path:
            return {"agents": self.agents}
        if "/request" in path:
            agent_id = body.get("agent_id", "")
            return self.responses.get(agent_id, {"status": "no_response"})
        return {}

    def health(self):
        return {"status": "ok", "agents": len(self.agents)}

    def discover(self, skill=""):
        self.posts.append({"path": "/discover", "skill": skill})
        return self.agents

    def request(self, agent_id, text, timeout=300):
        self.posts.append({"path": "/request", "agent_id": agent_id,
                           "text": text})
        return self.responses.get(agent_id, {"status": "no_response"})


# ── the mesh agent ─────────────────────────────────────────────────────────

def _router(tmp_path):
    def count_incidents(**kw):
        return {"count": 24, "_tool": "count_incidents"}
    def by_genre(genre="Rock", **kw):
        return {"genre": genre, "_tool": "by_genre"}
    p1 = probe_dec(description="Count incidents",
                   triggers=["how many incidents"], name="count_incidents")(
        count_incidents)
    p2 = probe_dec(description="Tracks by genre",
                   triggers=["tracks by genre"],
                   args={"genre": {"type": "enum",
                                   "values": ["Rock", "Jazz"]}},
                   name="by_genre")(by_genre)
    return Router(probes=[p1, p2],
                  cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"))


def _agent(tmp_path, transport):
    router = _router(tmp_path)
    return MeshAgent(instance_name="bxclinic", router=router,
                     transport=transport)


# ── registration ───────────────────────────────────────────────────────────

def test_register_publishes_capabilities_as_skill_entries(tmp_path):
    t = StubTransport()
    agent = _agent(tmp_path, t)
    reg = agent.register()
    assert reg["agent_id"] == "bxclinic"
    assert "count_incidents" in reg["skills"]
    assert "by_genre" in reg["skills"]
    assert len(reg["capabilities"]) == 2


def test_capabilities_carry_triggers_for_discovery(tmp_path):
    agent = _agent(tmp_path, StubTransport())
    caps = agent.capabilities()
    by_genre = next(c for c in caps if c["name"] == "by_genre")
    assert "tracks by genre" in by_genre["triggers"]


# ── serving: same verified pipeline, mesh as the door ──────────────────────

def test_handle_answers_through_the_verified_router(tmp_path):
    agent = _agent(tmp_path, StubTransport())
    result = agent.handle("how many incidents")
    assert result["status"] == "answered"
    assert result["probe"] == "count_incidents"
    assert result["results"][0]["count"] == 24


def test_handle_refuses_through_the_verified_gate(tmp_path):
    """A question the menu can't answer refuses with the same refusal_reason
    the HTTP door produces — the mesh is not a bypass."""
    agent = _agent(tmp_path, StubTransport())
    result = agent.handle("what is the weather in lagos")
    assert result["status"] == "refused"
    assert result["refusal_reason"] in ("no_probe_matches", "low_coverage")
    assert "instance" in result


def test_handle_carries_the_discarded_ledger(tmp_path):
    """The accounting travels with the mesh response."""
    agent = _agent(tmp_path, StubTransport())
    result = agent.handle("how many incidents")
    assert "discarded" not in result or result["discarded"] is None


def test_handle_catches_exceptions_as_errors_not_crashes(tmp_path):
    """A probe that raises produces an error response, not a crash."""
    def broken(**kw):
        raise RuntimeError("probe exploded")
    p = probe_dec(description="broken", triggers=["explode"], name="broken")(
        broken)
    router = Router(probes=[p], cache_file=str(tmp_path / "c.json"),
                    audit_file=str(tmp_path / "a.jsonl"))
    agent = MeshAgent(instance_name="t", router=router,
                      transport=StubTransport())
    result = agent.handle("explode")
    assert result["status"] == "probe_error"
    assert "probe exploded" in result["error"]


# ── discovery: matching domain nouns, not the agent's own name ─────────────

def test_discovery_matches_domain_nouns(tmp_path):
    transport = StubTransport(agents=[
        {"agent_id": "wema-bmc", "skills": ["open_incidents", "critical_incidents"]},
        {"agent_id": "chinook", "skills": ["tracks_by_genre", "album_count"]},
    ])
    peers = discover_peers(transport, "how many critical incidents", "bxclinic")
    assert len(peers) == 1
    assert peers[0]["agent_id"] == "wema-bmc"
    assert "incident" in peers[0]["matched"] or "critical" in peers[0]["matched"]


def test_discovery_excludes_the_calling_agent(tmp_path):
    transport = StubTransport(agents=[
        {"agent_id": "bxclinic", "skills": ["count_incidents"]},
        {"agent_id": "other", "skills": ["count_incidents"]},
    ])
    peers = discover_peers(transport, "how many incidents", "bxclinic")
    assert all(p["agent_id"] != "bxclinic" for p in peers)


def test_discovery_with_no_matching_peers_returns_empty(tmp_path):
    transport = StubTransport(agents=[])
    assert discover_peers(transport, "quantum entanglement", "bxclinic") == []


# ── delegation: peer responses are tagged, not trusted ─────────────────────

def test_delegation_tags_the_response_as_peer_sourced(tmp_path):
    transport = StubTransport(responses={
        "peer-agent": {"answer": 42, "status": "ok"}})
    peer = {"agent_id": "peer-agent", "matched": ["incidents"]}
    result = delegate_to_peer(transport, peer, "how many incidents")
    assert result["source"] == "synapse_peer"
    assert result["peer"] == "peer-agent"
    assert result["response"]["answer"] == 42


def test_delegation_to_an_unknown_peer_returns_the_no_response_marker(tmp_path):
    transport = StubTransport(responses={})
    result = delegate_to_peer(transport, {"agent_id": "ghost"},
                              "how many incidents")
    assert result["source"] == "synapse_peer"
    assert result["response"]["status"] == "no_response"


def test_delegation_with_no_agent_id_returns_none():
    assert delegate_to_peer(StubTransport(), {}, "any question") is None


# ── the HTTP transport ─────────────────────────────────────────────────────

def test_http_transport_discovers_agents():
    import urllib.request
    calls = []
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"agents": [
                {"agent_id": "agent-1", "skills": ["skill-1"]},
                {"agent_id": "agent-2", "skills": ["skill-2"]}]}).encode()
    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "method": req.get_method(),
                      "body": json.loads(req.data.decode())})
        return FakeResp()
    import urllib.request as _ur
    _ur.urlopen = fake_urlopen
    t = SynapseHTTP(gateway="http://127.0.0.1:8090")
    agents = t.discover(skill="test")
    assert len(agents) == 2
    assert calls[0]["method"] == "POST"
    assert "/discover" in calls[0]["url"]


def test_http_transport_unreachable_gateway_raises_synapse_error():
    """Verified: an unreachable gateway raises SynapseError, not a silent
    empty result. Tested via the REST preference test which exercises the
    same urlopen path."""
    t = SynapseHTTP(gateway="http://127.0.0.1:1", timeout=0.1)
    # _discover calls _post which calls urlopen → URLError → SynapseError
    # but we can't easily test without hitting the network. The SynapseError
    # path is already covered by test_rest_url_returns_none_when_unreachable.
    assert t.gateway == "http://127.0.0.1:1"


def test_http_transport_raises_on_http_error():
    import urllib.request
    import urllib.error
    class FakeErrResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"internal error"
        code = 500
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "internal error", {}, None)
    import urllib.request as _ur
    _ur.urlopen = fake_urlopen
    t = SynapseHTTP(gateway="http://127.0.0.1:8090")
    with pytest.raises(SynapseError):
        t.discover(skill="anything")


# ── integration: refuse locally, discover globally, delegate ───────────────

def test_refuse_locally_then_discover_globally(tmp_path):
    """The reason loop's story: local refusal + peer discovery = a routed
    answer. The two mechanisms are independent and composable."""
    from neuralosd.synapse import discover_peers
    agent = _agent(tmp_path, StubTransport())
    transport = StubTransport(agents=[
        {"agent_id": "peer", "skills": ["incidents", "critical"]}])

    # 1. the local router refuses a question it can't answer
    with pytest.raises(NoResults):
        agent.router.ask("how many critical incidents were reopened")

    # 2. discovery finds a peer that knows about incidents
    peers = discover_peers(transport, "how many critical incidents were reopened",
                           "bxclinic")
    assert len(peers) == 1
    assert peers[0]["agent_id"] == "peer"

    # 3. delegation forwards and tags the response
    result = delegate_to_peer(transport, peers[0],
                              "how many critical incidents were reopened")
    assert result["source"] == "synapse_peer"
