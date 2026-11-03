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
    "REACTORPRO_GATEWAY_URL",
    os.environ.get("NEURALOSD_SYNAPSE_URL", "http://127.0.0.1:3000"))
DEFAULT_TOKEN_PATH = os.path.expanduser(
    os.environ.get("REACTORPRO_GATEWAY_ENV",
                   "~/.config/reactorpro/gateway.env"))


class SynapseError(Exception):
    """The Synapse gateway is unreachable, unauthorized, or refused."""


def _http(url, headers=None, data=None, timeout=30.0, method=None):
    """The raw HTTP call. Module-level so tests (and exotic deployments)
    can inject it without touching the client."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise SynapseError(f"gateway {method or 'GET'} {url} -> "
                           f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise SynapseError(f"Synapse gateway unreachable at {url}: {e}") from e


# ── the HTTP transport ─────────────────────────────────────────────────────

class SynapseHTTP:
    """Thin client for the REAL synapse-gateway REST API.

    Every mesh operation goes through /api/mesh/* with a Bearer token —
    resolved from REACTORPRO_GATEWAY_TOKEN, or the gateway.env file
    (LIVEAGENT_GATEWAY_TOKEN=...) that a default gateway install writes.
    The token is read at ask time and never written into instance files.

    Every method raises SynapseError on connection failure or non-2xx — the
    caller decides whether to fall back or propagate.
    """

    def __init__(self, gateway: str = DEFAULT_GATEWAY,
                 timeout: float = 30.0, token: Optional[str] = None,
                 token_path: Optional[str] = None):
        self.gateway = gateway.rstrip("/")
        self.timeout = timeout
        self.token = token if token is not None else \
            self._resolve_token(token_path)

    @staticmethod
    def _resolve_token(path: Optional[str]) -> Optional[str]:
        env = os.environ.get("REACTORPRO_GATEWAY_TOKEN")
        if env:
            return env
        path = path or DEFAULT_TOKEN_PATH
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        if "=" in line and "TOKEN" in line.upper():
                            return line.split("=", 1)[1].strip().strip('"')
            except OSError:
                return None
        return None

    def _headers(self, json_body: bool = True) -> Dict[str, str]:
        h = {}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _call(self, method: str, path: str, body=None) -> Dict[str, Any]:
        url = f"{self.gateway}{path}"
        data = json.dumps(body).encode() if body is not None else None
        raw = _http(url, headers=self._headers(body is not None), data=data,
                    timeout=self.timeout, method=method)
        try:
            return json.loads(raw.decode())
        except ValueError:
            return {"raw": raw.decode(errors="replace")}

    def _get(self, path: str) -> Dict[str, Any]:
        return self._call("GET", path)

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", path, body)

    def health(self) -> Dict[str, Any]:
        try:
            return self._get("/api/mesh/health")
        except SynapseError:
            return self._get("/api/status")

    def status(self) -> Dict[str, Any]:
        return self._get("/api/mesh/status")

    def trust(self) -> Dict[str, Any]:
        return self._get("/api/mesh/trust")

    def agents(self, capabilities: str = "", availability: str = ""):
        """The peer directory (the phonebook). Filters are subset matches."""
        q = []
        if capabilities:
            q.append(f"capabilities={capabilities}")
        if availability:
            q.append(f"availability={availability}")
        data = self._get("/api/mesh/agents" + ("?" + "&".join(q) if q else ""))
        return data.get("agents", data)

    def discover(self, skill: str = "") -> List[Dict[str, Any]]:
        """Find agents that match a skill or capability."""
        agents = self.agents(capabilities=skill)
        return agents if isinstance(agents, list) else []

    def dispatch(self, target: str, text: str, skill: str = "invoke",
                 timeout_ms: int = 120000) -> Dict[str, Any]:
        """Ask a peer synchronously (the desktop's own MeshSend path).

        Minutes-scale by design: a real agent turn takes 10-60s+. The reply
        is UNTRUSTED REMOTE OUTPUT — payload.text first (bridges), then
        payload.output (ReactorPro edges wrap as output.result). Error
        envelopes raise SynapseError carrying the mesh error code.
        """
        resp = self._post("/api/mesh/dispatch", {
            "target": target, "skill": skill,
            "input": {"text": text}, "timeoutMs": timeout_ms})
        return _read_reply(resp)

    def request(self, agent_id: str, text: str,
                timeout: float = 300) -> Dict[str, Any]:
        """Backward-compatible shim: the raw respond envelope from dispatch."""
        return self._post("/api/mesh/dispatch", {
            "target": agent_id, "skill": "invoke",
            "input": {"text": text}, "timeoutMs": int(timeout * 1000)})

    # ── durable agent2agent tasks (at-least-once, idempotent) ────────────

    def create_task(self, target: str, text: str, task_id: Optional[str] = None,
                    skill: str = "invoke", **options) -> Dict[str, Any]:
        """Async CREATE — 202 + stub in ~2s. The caller-minted taskId is the
        IDEMPOTENCY KEY: a retry never runs the work twice."""
        import uuid
        body = {"target": target, "skill": skill,
                "input": {"text": text},
                "taskId": task_id or f"nosd-{uuid.uuid4().hex[:12]}"}
        body.update(options)
        return self._post("/api/mesh/tasks", body)

    def get_task(self, task_id: str, refresh: bool = False):
        suffix = "?refresh=true" if refresh else ""
        return self._get(f"/api/mesh/tasks/{task_id}{suffix}")

    def cancel_task(self, task_id: str):
        return self._post(f"/api/mesh/tasks/{task_id}/cancel", {})

    def retry_task(self, task_id: str):
        return self._post(f"/api/mesh/tasks/{task_id}/retry", {})

    def mailbox(self, target: str, skill: str = "status",
                input_data=None, task_id: Optional[str] = None):
        """Durable mailbox message — fire and FORGET (202, at-least-once,
        no reply by design). The skill must be idempotent."""
        body = {"target": target, "skill": skill,
                "input": input_data if input_data is not None else {}}
        if task_id:
            body["taskId"] = task_id
        return self._post("/api/mesh/mailbox", body)


def _read_reply(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Read a respond envelope. Never trust the shape: payload.text for
    bridges, payload.output for ReactorPro edges (which wrap as
    output.result). Error envelopes raise with the mesh error code."""
    if not isinstance(resp, dict):
        raise SynapseError(f"unrecognised gateway reply: {str(resp)[:200]}")
    err = resp.get("error")
    if isinstance(err, dict):
        raise SynapseError(f"mesh error {err.get('code')}: "
                           f"{err.get('message')}")
    if isinstance(err, str):
        raise SynapseError(f"mesh error: {err}")
    payload = resp.get("payload") or {}
    text = payload.get("text")
    if text is None:
        out = payload.get("output")
        if isinstance(out, dict) and set(out) == {"result"}:
            text = str(out["result"])
        elif out is not None:
            text = out if isinstance(out, str) else json.dumps(out)
    return {"text": text, "from": resp.get("from"), "raw": resp}


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
