"""Synapse mesh integration — neuralosd as a first-class citizen on the agent mesh.

The Synapse protocol (NATS-based) lets any agent discover, talk to, and
collaborate with any other agent. neuralosd's verified probes become callable
by any agent on the mesh, and when neuralosd hits a gap it can't close
locally, it asks the mesh before refusing.

Three capabilities:

  REGISTER  publish the probe menu to the mesh registry, so other agents can
            discover what this instance can answer.

  SERVE     subscribe to the agent inbox and handle requests via
            ``router.ask()`` — the same verified pipeline, the same refusal
            gate, the same accounting. The mesh is just another door.

  DISCOVER  find peer agents by capability, so the reason loop can route a
            gap to whoever has the answer instead of refusing.

The refusal gate and Synapse discovery solve complementary halves of the same
problem: the gate says "not me", Synapse says "but maybe them", and the
verification gate says "prove it before I trust it".

Transport: the synapse-gateway HTTP API (POST /discover, POST /request, GET
/health) is the default — it bridges HTTP-only clients to the NATS mesh
without requiring the nats-py dependency. A NATS-direct transport is the
extension point for production deployments that want to skip the gateway.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

DEFAULT_GATEWAY = os.environ.get(
    "NEURALOSD_SYNAPSE_URL", "http://127.0.0.1:8090")


class SynapseError(Exception):
    """The Synapse gateway is unreachable or returned an error."""


# ── the HTTP transport ─────────────────────────────────────────────────────

class SynapseHTTP:
    """Thin client for the synapse-gateway REST API.

    Every method raises SynapseError on connection failure or non-2xx — the
    caller decides whether to fall back or propagate.
    """

    def __init__(self, gateway: str = DEFAULT_GATEWAY,
                 timeout: float = 30.0):
        self.gateway = gateway.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.gateway}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise SynapseError(f"Synapse {path} returned {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise SynapseError(f"Synapse gateway unreachable at {self.url}: {e}") from e

    def health(self) -> Dict[str, Any]:
        url = f"{self.gateway}/health"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:                        # noqa: BLE001
            raise SynapseError(f"Synapse health check failed: {e}") from e

    def discover(self, skill: str = "") -> List[Dict[str, Any]]:
        """Find agents that match a skill or capability."""
        result = self._post("/discover", {"skill": skill})
        agents = result.get("agents") or result.get("results") or result
        return agents if isinstance(agents, list) else []

    def request(self, agent_id: str, text: str,
                timeout: float = 300) -> Dict[str, Any]:
        """Send a request to a specific agent."""
        return self._post("/request", {"agent_id": agent_id, "text": text,
                                       "timeout": timeout})


# ── the mesh agent ─────────────────────────────────────────────────────────

@dataclass
class MeshAgent:
    """ neuralosd as a first-class citizen on the Synapse mesh.

    Wraps a Router (the verified pipeline) and exposes it on the mesh:
    the probe menu becomes the agent's declared capabilities, and incoming
    requests go through the same router.ask() as the HTTP door.
    """
    instance_name: str
    router: Any                                      # neuralosd Router
    transport: SynapseHTTP
    skills: List[str] = field(default_factory=list)

    def capabilities(self) -> List[Dict[str, Any]]:
        """The probe menu as mesh-registry skill entries."""
        return [{"name": m.name, "description": m.description,
                 "triggers": m.triggers,
                 "instance": self.instance_name}
                for m in self.router.metas]

    def register(self) -> Dict[str, Any]:
        """Publish this instance's capabilities to the mesh registry."""
        caps = self.capabilities()
        self.skills = [c["name"] for c in caps]
        return {"agent_id": self.instance_name, "skills": self.skills,
                "capabilities": caps,
                "transport": "neuralosd",
                "door": f"neuralosd ask --instance-dir <dir>"}

    def handle(self, question: str) -> Dict[str, Any]:
        """Handle an incoming mesh request through the verified pipeline.

        Same router, same refusal gate, same accounting. A question that
        fails the calibration checks refuses honestly — the mesh caller gets
        the same refusal_reason and score vector the HTTP door produces.
        """
        from .router import NoResults
        try:
            env = self.router.ask(question)
            # A probe that raises produces an error envelope, not an exception.
            # Surface it so the mesh caller knows the answer is not usable.
            mode = env.get("mode", "")
            if mode.endswith("-error"):
                err_detail = ""
                if isinstance(env.get("results"), list) and env["results"]:
                    err_detail = env["results"][0].get("error", "")
                return {"status": "probe_error",
                        "error": err_detail or f"probe returned {mode}",
                        "instance": self.instance_name}
            return {"status": "answered",
                    "probe": env.get("probe"),
                    "results": env.get("results"),
                    "discarded": env.get("discarded"),
                    "instance": self.instance_name}
        except NoResults as e:
            env = getattr(e, "envelope", {}) or {}
            return {"status": "refused",
                    "refusal_reason": env.get("refusal_reason"),
                    "error": env.get("error"),
                    "instance": self.instance_name}
        except Exception as e:                        # noqa: BLE001
            return {"status": "error", "error": str(e),
                    "instance": self.instance_name}


# ── discovery: find peers that can answer what we can't ────────────────────

def discover_peers(transport: SynapseHTTP, question: str,
                   own_name: str) -> List[Dict[str, Any]]:
    """Find mesh agents whose skills match the question's domain nouns.

    Used by the reason loop after a local refusal: if another agent has the
    capability, neuralosd can relay the question instead of just refusing.
    The caller decides whether to trust the response — Synapse discovery is
    a hint, not a verification.
    """
    import re
    from .router import GENERIC_INTENT
    words = set(re.findall(r"[a-z0-9_]+", question.lower())) - GENERIC_INTENT
    if not words:
        return []
    agents = transport.discover(skill="")
    out = []
    for agent in agents:
        agent_id = agent.get("agent_id") or agent.get("id") or ""
        skills = agent.get("skills") or []
        skills_text = " ".join(str(s) for s in skills).lower()
        overlap = words & set(re.findall(r"[a-z0-9]+", skills_text.replace("_", " ")))
        if overlap and agent_id != own_name:
            out.append({"agent_id": agent_id, "matched": sorted(overlap),
                        "skills": skills})
    return out


def delegate_to_peer(transport: SynapseHTTP, peer: Dict[str, Any],
                     question: str) -> Optional[Dict[str, Any]]:
    """Forward a question to a peer agent and return its response.

    The peer's answer is NOT verified by neuralosd — the caller must check
    the response before using it. The `source` field marks it as a peer
    response so downstream consumers know it didn't come from a local probe.
    """
    agent_id = peer.get("agent_id")
    if not agent_id:
        return None
    try:
        result = transport.request(agent_id, question)
        return {"source": "synapse_peer", "peer": agent_id,
                "response": result}
    except SynapseError:
        return None
