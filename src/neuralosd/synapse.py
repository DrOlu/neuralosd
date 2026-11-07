"""Synapse mesh integration — neuralosd as a first-class citizen on the agent mesh.

The Synapse protocol (NATS-based, HTTP-gatewayed) lets any agent discover,
talk to, and collaborate with any other agent. neuralosd's verified probes
become callable by any agent on the mesh, and when neuralosd hits a gap it
can't close locally, it asks the mesh before refusing.

Five capabilities:

  REGISTER  publish a versioned registration (menu_version + gate_version +
            mesh_protocol_version + the probe menu) so peers can discover
            what this instance can answer, and re-assert it on a heartbeat.

  SERVE     handle requests via ``router.ask()`` — the same verified
            pipeline, the same refusal gate, the same accounting. Answers
            carry provenance (versions + answer hash + signature) and are
            redacted per the caller's trust class; the mesh is just another
            door, and it is not a bypass.

  PREFLIGHT ``router.dry_run()``: would this question be answered, by which
            probe, under which gate — without running a probe, touching the
            cache, or writing the audit log. Peers ask this BEFORE paying
            for a round trip.

  DISCOVER  find peer agents with the SAME scorer that routes local
            questions (score_probe over the peers' published menus), so
            discovery and routing cannot disagree about what "matches".
            Optional preflight filters out peers that would only refuse.

  DELEGATE  forward a refused question to the best peer — refusal-aware
            (a peer refusal is surfaced as a refusal, never relabelled as
            an answer), loop-safe (visited set), hop-limited, and
            deadline-bounded. Peer answers are verified against the peer's
            advertised capabilities before they are tagged usable.

The refusal gate and Synapse discovery solve complementary halves of the
same problem: the gate says "not me", Synapse says "but maybe them", and
the verification gate says "prove it before I trust it".

Trust classes: ``self`` (full ledger), ``trusted`` (scores + ledger),
``known`` (default: verdicts + confidence band, no score vector),
``untrusted`` (verdict only, delegation denied). Redaction happens BEFORE
signing, so a signature never attests something that was withheld.

Gossip: gap fingerprints stay hash-only on the wire (no questions leave
the edge). ``propose_triggers`` turns a local refusal log into trigger
proposals; ``explain_fingerprints`` (in loop.py) lets the OWNER of a gap
log explain its own hashes to a trusted peer — the honest two-way loop.

Transport: the synapse-gateway HTTP API is the default — it bridges
HTTP-only clients to the NATS mesh without requiring the nats-py
dependency. Idempotent calls (GETs, and POSTs carrying an idempotency key)
are retried with backoff on transient failures; deadline-aware timeouts
keep a delegation chain inside its budget. A NATS-direct transport remains
the extension point for production deployments that want to skip the
gateway.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .router import (GATE_VERSION, GENERIC_INTENT, REFUSAL_REASONS,
                     score_probe, tokens)

# ── the mesh contract, versioned ───────────────────────────────────────────
# MESH_PROTOCOL_VERSION names the ENVELOPE shape (provenance keys, signature
# block, preflight/delegation framing); GATE_VERSION (from router.py) names
# the refusal semantics. A peer that only understands protocol 1 must never
# be handed a protocol-2 answer silently — handle() refuses the downgrade
# and discovery annotates the mismatch.
MESH_PROTOCOL_VERSION = 1
SUPPORTED_MESH_PROTOCOLS = (1,)

DEFAULT_GATEWAY = os.environ.get(
    "REACTORPRO_GATEWAY_URL",
    os.environ.get("NEURALOSD_SYNAPSE_URL", "http://127.0.0.1:3000"))
DEFAULT_TOKEN_PATH = os.path.expanduser(
    os.environ.get("REACTORPRO_GATEWAY_ENV",
                   "~/.config/reactorpro/gateway.env"))

HEARTBEAT_SECONDS = 30.0
MAX_HOPS = 3
DEFAULT_DEADLINE_MS = 120_000
SIG_KEY_ENV = "NEURALOSD_MESH_KEY"

# Transient failure codes a retry may heal. Everything else (auth, 404,
# bad reply) is a decision, not a flake — retrying it would just be slow.
RETRYABLE_CODES = frozenset({"unreachable", "timeout", "rate_limited",
                             "gateway_error"})


class SynapseError(Exception):
    """The Synapse gateway is unreachable, unauthorized, or refused.

    ``code`` is a bare token callers can branch on without parsing prose:
    unreachable | timeout | unauthorized | not_found | rate_limited |
    gateway_error | mesh_error.
    """

    def __init__(self, message: str, code: str = "mesh_error",
                 status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _error_code(status: Optional[int]) -> str:
    if status in (401, 403):
        return "unauthorized"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "gateway_error"
    return "mesh_error"


def _http(url, headers=None, data=None, timeout=30.0, method=None):
    """The raw HTTP call. Module-level so tests (and exotic deployments)
    can inject it without touching the client."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:                       # noqa: BLE001 — body unreadable
            detail = ""
        raise SynapseError(f"gateway {method or 'GET'} {url} -> "
                           f"HTTP {e.code}: {detail}",
                           code=_error_code(e.code), status=e.code) from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, (TimeoutError, )) or \
                "timed out" in str(reason or "").lower():
            raise SynapseError(f"Synapse gateway timed out at {url}: {e}",
                               code="timeout") from e
        raise SynapseError(f"Synapse gateway unreachable at {url}: {e}",
                           code="unreachable") from e
    except OSError as e:
        raise SynapseError(f"Synapse gateway unreachable at {url}: {e}",
                           code="unreachable") from e


# ── deadlines ──────────────────────────────────────────────────────────────

@dataclass
class Deadline:
    """A monotonic budget that travels WITH a delegated question.

    A chain of peers must not outlive the patience of whoever asked first:
    every hop inherits what's left of the original budget, so A→B→C cannot
    quietly take three timeouts to answer one user.
    """
    budget_ms: int = DEFAULT_DEADLINE_MS
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def from_ms(cls, ms: Optional[float]) -> "Deadline":
        return cls(budget_ms=max(0, int(ms)) if ms is not None
                   else DEFAULT_DEADLINE_MS)

    def remaining_ms(self) -> int:
        used = (time.monotonic() - self.started) * 1000.0
        return max(0, int(self.budget_ms - used))

    def remaining_s(self) -> float:
        return self.remaining_ms() / 1000.0

    def expired(self) -> bool:
        return self.remaining_ms() <= 0


# ── trust classes ──────────────────────────────────────────────────────────

TRUST_LEVELS = ("self", "trusted", "known", "untrusted")


@dataclass(frozen=True)
class TrustPolicy:
    """What a caller may see, decided once and applied uniformly.

    Verdicts (status, refusal_reason, confidence band) are the *decision*
    the gate made and always travel. The score vector is HOW the decision
    was made — menu vocabulary in ranking order — so it only crosses a
    trust boundary on purpose. Same rule for the discarded-filter ledger:
    useful to a trusted orchestrator, a probing surface to a stranger.
    """
    level: str = "known"
    expose_scores: bool = False
    expose_candidates: bool = False
    expose_discarded: bool = False
    allow_delegation: bool = True

    @classmethod
    def for_level(cls, level: Optional[str]) -> "TrustPolicy":
        if isinstance(level, TrustPolicy):
            return level
        level = (level or "known").lower()
        if level not in TRUST_LEVELS:
            level = "untrusted"
        return {
            "self": cls("self", True, True, True, True),
            "trusted": cls("trusted", True, True, True, True),
            "known": cls("known", False, False, False, True),
            "untrusted": cls("untrusted", False, False, False, False),
        }[level]


def confidence_band(score: Optional[float], floor: Optional[float]) \
        -> Optional[str]:
    """A coarse band instead of a raw score: the CALLER can still tell a
    confident answer from a marginal one, but cannot reconstruct the menu
    vocabulary the ranking implied. High = at least twice the gate floor,
    medium = above it, low = below."""
    if score is None or score <= 0:
        return None
    f = float(floor) if floor else 3.0
    if score >= 2.0 * f:
        return "high"
    if score >= f:
        return "medium"
    return "low"


# ── provenance: hash + signature over the answer body ─────────────────────

_HASH_EXCLUDE = frozenset({"signature", "ask_id", "ts", "latency_ms",
                           "cached", "ms"})


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False).encode("utf-8")


def answer_hash(body: Any) -> str:
    """A stable digest of WHAT was answered, bound to the question and both
    protocol versions. Volatile fields (timestamps, ids, the signature
    itself) are excluded so a retry of the same answer hashes equal."""
    sub = {k: v for k, v in (body or {}).items() if k not in _HASH_EXCLUDE}
    return hashlib.sha256(_canonical(sub)).hexdigest()


def _key_id(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:8]


def sign_envelope(body: Any, key: Optional[str] = None,
                  kid: Optional[str] = None) -> Dict[str, Any]:
    """A signature block for ``body``.

    With a key: HMAC-SHA256 (authenticity — the peer can prove the answer
    came from a holder of the shared secret). Without: bare SHA-256
    (integrity only — a checksum, not an identity). The distinction is
    carried in ``authenticated`` so a verifier can never mistake one for
    the other.
    """
    digest = answer_hash(body)
    if key:
        sig = hmac.new(str(key).encode("utf-8"), digest.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return {"alg": "hmac-sha256", "kid": kid or _key_id(key),
                "hash": digest, "sig": sig, "authenticated": True}
    return {"alg": "sha256", "kid": None, "hash": digest, "sig": digest,
            "authenticated": False}


def verify_signature(body: Any, key: Optional[str] = None) \
        -> (bool, str):
    """Re-verify an answer's signature block. Returns (ok, reason).

    ``integrity_only`` means the hash matches but nobody attests to it —
    good enough to detect corruption, never enough to detect forgery.
    """
    sig = (body or {}).get("signature")
    if not isinstance(sig, dict):
        return False, "unsigned"
    if sig.get("hash") != answer_hash(body):
        return False, "tampered"
    alg = str(sig.get("alg") or "")
    if alg == "hmac-sha256":
        if not key:
            return False, "no_key"
        want = hmac.new(str(key).encode("utf-8"),
                        str(sig.get("hash", "")).encode("utf-8"),
                        hashlib.sha256).hexdigest()
        if hmac.compare_digest(want, str(sig.get("sig", ""))):
            return True, "signature_ok"
        return False, "bad_signature"
    if alg == "sha256":
        return True, "integrity_only"
    return False, "unknown_alg"


# ── the HTTP transport ─────────────────────────────────────────────────────

class SynapseHTTP:
    """Thin client for the REAL synapse-gateway REST API.

    Every mesh operation goes through /api/mesh/* with a Bearer token —
    resolved from REACTORPRO_GATEWAY_TOKEN, or the gateway.env file
    (LIVEAGENT_GATEWAY_TOKEN=...) that a default gateway install writes.
    The token is read at ask time and never written into instance files.

    Every method raises SynapseError on connection failure or non-2xx — the
    caller decides whether to fall back or propagate. GETs (and POSTs that
    carry an idempotency key, e.g. the caller-minted taskId on a durable
    task) are retried with backoff on transient failures; every other POST
    fails fast, because retrying a non-idempotent call can double the work.
    """

    def __init__(self, gateway: str = DEFAULT_GATEWAY,
                 timeout: float = 30.0, token: Optional[str] = None,
                 token_path: Optional[str] = None, retries: int = 1):
        self.gateway = gateway.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, int(retries))
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

    def _timeout_for(self, deadline: Optional[Deadline]) -> float:
        if deadline is None:
            return self.timeout
        return max(0.1, min(self.timeout, deadline.remaining_s()))

    def _call(self, method: str, path: str, body=None,
              deadline: Optional[Deadline] = None,
              idempotency_key: Optional[str] = None,
              retries: Optional[int] = None) -> Dict[str, Any]:
        headers = self._headers(body is not None)
        data = json.dumps(body).encode() if body is not None else None
        url = f"{self.gateway}{path}"
        allow = self.retries if retries is None else max(0, int(retries))
        idem = idempotency_key or (body.get("taskId")
                                   if isinstance(body, dict) else None)
        if idem:
            # The key travels on the wire: a gateway that retries on its
            # side must collapse onto the same work, not duplicate it.
            headers["Idempotency-Key"] = str(idem)
        if method != "GET" and not idem:
            allow = 0          # not provably safe to repeat — fail fast
        attempt = 0
        while True:
            try:
                raw = _http(url, headers=headers, data=data,
                            timeout=self._timeout_for(deadline),
                            method=method)
                try:
                    return json.loads(raw.decode())
                except ValueError:
                    return {"raw": raw.decode(errors="replace")}
            except SynapseError as exc:
                if attempt >= allow or exc.code not in RETRYABLE_CODES:
                    raise
                attempt += 1
                time.sleep(min(2.0, 0.25 * (2 ** attempt)))

    def _get(self, path: str, **kw) -> Dict[str, Any]:
        return self._call("GET", path, **kw)

    def _post(self, path: str, body: Dict[str, Any], **kw) -> Dict[str, Any]:
        return self._call("POST", path, body, **kw)

    def health(self) -> Dict[str, Any]:
        try:
            return self._get("/api/mesh/health")
        except SynapseError as exc:
            if exc.code == "not_found":
                return self._get("/api/status")
            raise

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

    # ── preflight / registration / gossip (protocol v1 additions) ────────

    def preflight(self, target: str, text: str,
                  deadline: Optional[Deadline] = None) -> Dict[str, Any]:
        """Ask a peer WOULD-YOU-ANSWER before paying for the answer.

        No probe runs on the peer; the reply is its gate's verdict. An old
        gateway that does not implement the verb answers 404 → SynapseError
        (not_found) and the caller degrades gracefully to unverified
        discovery.
        """
        return self._post("/api/mesh/preflight", {
            "target": target, "meshProtocolVersion": MESH_PROTOCOL_VERSION,
            "input": {"text": text}},
            deadline=deadline,
            idempotency_key=f"pf-{uuid.uuid4().hex[:12]}",
            retries=0)

    def register(self, registration: Dict[str, Any]) -> Dict[str, Any]:
        """Publish a registration blob. Best-effort by contract: a gateway
        that has no registry endpoint raises not_found and the caller
        keeps serving locally."""
        return self._post("/api/mesh/agents", registration)

    def heartbeat(self, agent_id: str,
                  registration: Optional[Dict[str, Any]] = None,
                  deadline: Optional[Deadline] = None) -> Dict[str, Any]:
        """Re-assert liveness + the current menu digest."""
        body = dict(registration or {"agent_id": agent_id})
        body["agent_id"] = agent_id
        body["ts"] = time.time()
        return self._post(f"/api/mesh/agents/{agent_id}/heartbeat", body,
                          deadline=deadline,
                          idempotency_key=f"hb-{uuid.uuid4().hex[:12]}")

    # ── durable agent2agent tasks (at-least-once, idempotent) ────────────

    def create_task(self, target: str, text: str, task_id: Optional[str] = None,
                    skill: str = "invoke", **options) -> Dict[str, Any]:
        """Async CREATE — 202 + stub in ~2s. The caller-minted taskId is the
        IDEMPOTENCY KEY: a retry never runs the work twice."""
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


# ── peer-answer verification ───────────────────────────────────────────────

def verify_peer_answer(response: Any, capabilities: Optional[Iterable] = None,
                       expected_menu_version: Optional[str] = None,
                       expected_gate_version: Optional[int] = None) \
        -> Dict[str, Any]:
    """Check a peer's answer against what the peer ADVERTISED.

    A refusal is not a failure of verification — it is a verdict, and it
    travels honestly (valid=False ONLY in the sense that there is no answer
    to use; reason=peer_refused). Verification catches the dangerous cases:
    an answer claiming a probe the peer never published, a probe that ran
    and produced nothing, a stale menu, or a gate the local side cannot
    interpret.
    """
    reasons: List[str] = []
    if not isinstance(response, dict):
        return {"valid": False, "reasons": ["not_a_dict"], "status": None}
    status = str(response.get("status") or "").lower()
    if status == "refused":
        return {"valid": False, "reasons": ["peer_refused"], "status": status}
    if not status:
        return {"valid": False, "reasons": ["missing_status"],
                "status": None}
    if status not in ("answered", "answered_unverified"):
        return {"valid": False, "reasons": [f"status_{status}"],
                "status": status or None}
    probe = response.get("probe")
    if not isinstance(probe, str) or not probe:
        reasons.append("missing_probe")
    results = response.get("results")
    if not results or results in (None, [], {}):
        reasons.append("empty_results")
    if capabilities is not None and isinstance(probe, str):
        names = set()
        for c in capabilities:
            if isinstance(c, dict):
                n = c.get("name")
                if n:
                    names.add(str(n))
            elif c:
                names.add(str(c))
        if names and probe not in names:
            reasons.append("unknown_probe")
    peer_menu = response.get("menu_version")
    if expected_menu_version and peer_menu and \
            str(peer_menu) != str(expected_menu_version):
        reasons.append("stale_menu")
    peer_gate = response.get("gate_version")
    if expected_gate_version is not None and peer_gate is not None and \
            int(peer_gate) != int(expected_gate_version):
        reasons.append("gate_version_mismatch")
    peer_proto = response.get("mesh_protocol_version")
    if peer_proto is not None:
        try:
            if int(peer_proto) not in SUPPORTED_MESH_PROTOCOLS:
                reasons.append("protocol_unsupported")
        except (TypeError, ValueError):
            reasons.append("protocol_unparsable")
    return {"valid": not reasons, "reasons": reasons, "status": status}


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
    trust: Any = "known"                             # str or TrustPolicy
    signing_key: Optional[str] = None

    def __post_init__(self):
        if self.signing_key is None:
            self.signing_key = os.environ.get(SIG_KEY_ENV) or None
        if not isinstance(self.trust, TrustPolicy):
            self.trust = TrustPolicy.for_level(self.trust)

    # -- the menu, published three ways ------------------------------------

    def capabilities(self) -> List[Dict[str, Any]]:
        """The probe menu as mesh-registry skill entries.

        Triggers and descriptions are PUBLISHED DELIBERATELY: they are what
        peers score their questions against (the same words the local gate
        ranks by), so a peer's discovery and our own routing agree on what
        "matches". Argument specs are published as types only — the enum
        VALUE lists stay local.
        """
        out = []
        for m in self.router.metas:
            args = {an: {"type": spec.get("type", "string"),
                         "required": bool(spec.get("required", True))}
                    for an, spec in (m.args or {}).items()}
            out.append({"name": m.name, "description": m.description,
                        "triggers": list(m.triggers or []),
                        "args": args,
                        "instance": self.instance_name})
        return out

    def registration(self) -> Dict[str, Any]:
        """The full registration blob: capabilities + the versioned
        contract. Peers pin the versions so they can detect drift instead
        of silently misreading a changed menu or gate."""
        caps = self.capabilities()
        self.skills = [c["name"] for c in caps]
        return {"agent_id": self.instance_name,
                "skills": list(self.skills),
                "capabilities": caps,
                "transport": "neuralosd",
                "door": "neuralosd ask --instance-dir <dir>",
                "mesh_protocol_version": MESH_PROTOCOL_VERSION,
                "gate_version": GATE_VERSION,
                "menu_version": self.router.menu_version,
                "probes": len(caps),
                "heartbeat_seconds": HEARTBEAT_SECONDS,
                "ts": time.time()}

    def register(self) -> Dict[str, Any]:
        """Build this instance's registration blob.

        Publishing to the gateway is a separate, explicit call
        (``transport.register(reg())``) so the blob can be inspected,
        signed, or carried over any transport without a hidden network
        side effect inside a getter-shaped method.
        """
        return self.registration()

    def digest(self) -> Dict[str, Any]:
        """The menu WITHOUT the vocabulary — what gossip carries when a peer
        only needs to know that we changed, not what we know."""
        return {"agent_id": self.instance_name,
                "menu_version": self.router.menu_version,
                "gate_version": GATE_VERSION,
                "mesh_protocol_version": MESH_PROTOCOL_VERSION,
                "probes": len(self.router.metas)}

    # -- heartbeats: liveness + drift detection ------------------------------

    def heartbeat(self, previous: Optional[Dict[str, Any]] = None) \
            -> Dict[str, Any]:
        """One beat: the current registration, annotated with drift against
        the previous one. A changed menu_version here is the earliest
        possible signal that a peer's cached capabilities went stale —
        hours before any of its questions start refusing."""
        reg = self.registration()
        prev_menu = (previous or {}).get("menu_version")
        reg["changed"] = bool(prev_menu and prev_menu != reg["menu_version"])
        if prev_menu:
            reg["previous_menu_version"] = prev_menu
        return reg

    def heartbeat_loop(self, interval: float = HEARTBEAT_SECONDS,
                       max_beats: Optional[int] = None,
                       publish: bool = False):
        """Re-assert the registration on a cadence. Bounded by ``max_beats``
        so tests (and supervised scripts) cannot loop forever; interrupted
        cleanly on KeyboardInterrupt. ``publish=True`` pushes each beat to
        the gateway; otherwise the beats are yielded locally (drift
        detection works even when the gateway is down).

        Returns the list of beats.
        """
        beats, prev = [], None
        beat_no = 0
        try:
            while max_beats is None or beat_no < max_beats:
                beat = self.heartbeat(prev)
                beat["beat"] = beat_no + 1
                beats.append(beat)
                if publish and self.transport is not None:
                    try:
                        self.transport.heartbeat(self.instance_name, beat)
                        beat["published"] = True
                    except (SynapseError, AttributeError, TypeError) as exc:
                        beat["published"] = False
                        # A transport whose heartbeat attr is None (an old
                        # transport) raises TypeError, not AttributeError.
                        beat["publish_error"] = (getattr(exc, "code", None)
                                                 or "unsupported")
                if beat.get("changed"):
                    print(f"mesh: menu changed "
                          f"({prev} -> {beat['menu_version']})", flush=True)
                prev = beat
                beat_no += 1
                if max_beats is None or beat_no < max_beats:
                    time.sleep(interval)
        except KeyboardInterrupt:
            pass
        return beats

    # -- preflight: the would-you-answer verb -------------------------------

    def preflight(self, question: str) -> Dict[str, Any]:
        """Would this question be answered — WITHOUT running a probe.

        The decision half of the router, published as a mesh verb. No probe
        executes, the cache is untouched, the audit log is untouched: a
        preflight costs the caller nothing to lie about and costs us
        nothing to answer.
        """
        dr = self.router.dry_run(question)
        body = {"instance": self.instance_name,
                "mesh_protocol_version": MESH_PROTOCOL_VERSION,
                "menu_version": dr["menu_version"],
                "gate_version": GATE_VERSION,
                "would": dr["would"],
                "would_answer": dr["would_answer"],
                "probe": dr["probe"],
                "refusal_reason": dr["refusal_reason"],
                "candidates": dr["candidates"],
                "confidence_band": confidence_band(
                    (dr["scores"] or [{}])[0].get("score"),
                    getattr(self.router, "floor", None)),
                "ms": dr["ms"]}
        body["signature"] = sign_envelope(body, self.signing_key)
        return body

    # -- serving: same verified pipeline, mesh as the door -------------------

    def handle(self, question: str, trust: Optional[str] = None,
               mesh_protocol_version: Optional[int] = None) \
            -> Dict[str, Any]:
        """Handle an incoming mesh request through the verified pipeline.

        Same router, same refusal gate, same accounting. The answer carries
        provenance (menu_version + gate_version + mesh_protocol_version +
        answer hash + signature) and is redacted per the CALLER'S trust
        class BEFORE signing — a signature never attests what was withheld.

        A question that fails the calibration checks refuses honestly — the
        mesh caller gets the same refusal_reason the HTTP door produces
        (and the score vector only if its trust class earns it).
        """
        from .router import NoResults
        policy = self.trust if trust is None else TrustPolicy.for_level(trust)

        # Protocol negotiation: never silently downgrade. A caller that
        # speaks a NEWER envelope than we support would misparse whatever we
        # send; refusing the request is the only honest answer.
        if mesh_protocol_version is not None and \
                int(mesh_protocol_version) not in SUPPORTED_MESH_PROTOCOLS:
            return {"status": "incompatible",
                    "error": f"mesh protocol {mesh_protocol_version} not "
                             f"supported (this instance speaks "
                             f"{MESH_PROTOCOL_VERSION})",
                    "error_code": "mesh_protocol_mismatch",
                    "mesh_protocol_version": MESH_PROTOCOL_VERSION,
                    "instance": self.instance_name}
        if not isinstance(question, str):
            return {"status": "error",
                    "error": f"question must be a str, got "
                             f"{type(question).__name__}",
                    "error_code": "bad_question",
                    "mesh_protocol_version": MESH_PROTOCOL_VERSION,
                    "instance": self.instance_name}

        body = {"mesh_protocol_version": MESH_PROTOCOL_VERSION,
                "instance": self.instance_name, "question": question}
        try:
            env = self.router.ask(question)
            mode = env.get("mode", "")
            if mode.endswith("-error"):
                err_detail = ""
                if isinstance(env.get("results"), list) and env["results"]:
                    err_detail = env["results"][0].get("error", "")
                body.update({"status": "probe_error",
                             "probe": env.get("probe"),
                             "error": err_detail or f"probe returned {mode}",
                             "error_code": "probe_error"})
            else:
                body.update({"status": "answered",
                             "probe": env.get("probe"),
                             "results": env.get("results"),
                             "discarded": env.get("discarded"),
                             "scores": env.get("scores"),
                             "candidates": env.get("scores")})
                # An answered envelope does not carry the score vector (the
                # ranking is decision-making, not the answer). Recompute it
                # through the decision half — deterministic, no probe run —
                # so the confidence band and the trusted-ledger view rest on
                # the SAME numbers the gate actually decided with.
                if body["scores"] is None:
                    dr = self.router.dry_run(question)
                    body["scores"] = dr["scores"]
                    body["candidates"] = dr["scores"]
        except NoResults as e:
            env = getattr(e, "envelope", {}) or {}
            body.update({"status": "refused",
                         "refusal_reason": env.get("refusal_reason"),
                         "error": env.get("error"),
                         "scores": env.get("scores"),
                         "discarded": env.get("discarded")})
        except Exception as e:                        # noqa: BLE001
            body.update({"status": "error", "error": str(e),
                         "error_code": "internal_error"})
            return self._finish(body, policy)

        # The versions travel with every verdict: a peer that cached this
        # answer can detect menu drift instead of reusing a stale one.
        body["menu_version"] = self.router.menu_version
        body["gate_version"] = GATE_VERSION
        return self._finish(body, policy)

    def _finish(self, body: Dict[str, Any], policy: TrustPolicy) \
            -> Dict[str, Any]:
        """Apply the trust ledger, then provenance, in that order."""
        # The band is computed BEFORE the vector is withheld: redaction may
        # hide HOW confident we are to a stranger, never lie about it.
        scores = body.get("scores") or []
        top = (scores[0].get("score")
               if scores and isinstance(scores[0], dict) else None)
        # Record WHAT we are about to withhold before withholding it, so the
        # redacted view can be labelled as redacted rather than merely empty.
        withheld_discarded = (not policy.expose_discarded
                              and body.get("discarded"))
        if not policy.expose_scores:
            body.pop("scores", None)
        if not policy.expose_candidates:
            body.pop("candidates", None)
        if not policy.expose_discarded:
            body.pop("discarded", None)
        if policy.level in ("known", "untrusted"):
            body["confidence_band"] = confidence_band(
                top, getattr(self.router, "floor", None))
        if withheld_discarded and body.get("status") == "answered":
            # A redacted ledger must not LOOK like a complete one: the
            # receiver has to know the accounting was withheld, not absent.
            body["discarded"] = None
            body["ledger"] = "withheld"
        if not policy.allow_delegation:
            body["delegation_allowed"] = False
        body["ts"] = time.time()
        body["signature"] = sign_envelope(body, self.signing_key)
        return body

    # -- gossip: what we would extend, hashed -------------------------------

    def gossip_blob(self, audit_path: Optional[str] = None) -> Dict[str, Any]:
        """The outbound gossip payload for this instance: gap fingerprints
        (hash-only) + the menu digest. Questions never leave the edge."""
        from .loop import cluster_gaps, export_fingerprints
        gaps = refusals_from_audit(audit_path or self.router.audit_file)
        blob = export_fingerprints(cluster_gaps(gaps))
        blob["origin"] = self.instance_name
        blob["menu"] = self.digest()
        return blob


# ── refusal-log plumbing (local half of the gossip loop) ──────────────────

def refusals_from_audit(path: str, limit: int = 2000) -> List[str]:
    """The refused questions from an ask_audit.jsonl — the raw material for
    trigger proposals. Malformed lines are skipped; the newest ``limit``
    entries win."""
    out: List[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("mode") == "refused" or rec.get("refusal_reason"):
                    q = rec.get("question")
                    if isinstance(q, str) and q.strip():
                        out.append(q.strip())
    except OSError:
        return []
    return out[-limit:]


def propose_triggers(router: Any, gaps: Iterable[str], min_count: int = 2,
                     max_proposals: int = 10) -> List[Dict[str, Any]]:
    """Turn clustered refusals into CONCRETE trigger proposals.

    The missing-triggers half of menu gossip, computed locally: the router
    clusters the refused questions (loop.cluster_gaps), scores each cluster
    against the CURRENT menu with the same scorer that routes questions,
    and reports the nearest neighbour WITH THE EVIDENCE an owner needs to
    decide:

      covered        the cluster's core terms are already inside one of the
                     probe's triggers (refusals were gate-level, not
                     routing-level — look at the refusal reasons instead)
      extend_trigger every core term is known SOMEWHERE in the menu — the
                     subject exists, the phrasing does not; extending the
                     nearest probe's triggers is plausible
      new_subject    some core term no probe knows — no trigger on an
                     existing probe can honestly answer this; the menu
                     needs a capability, and a trigger would only teach
                     the router to answer a question the probe cannot

    Proposals are made, never applied: a trigger changes what the gate
    refuses, and that decision belongs to the instance's owner.
    """
    from .loop import cluster_gaps
    if not isinstance(gaps, (list, tuple)):
        gaps = list(gaps)
    clusters = cluster_gaps([{"question": g} for g in gaps if g])
    proposals = []
    for c in clusters:
        if c["size"] < min_count:
            continue
        core = [t for t in (c.get("common_terms") or [])
                if t not in GENERIC_INTENT]
        if not core:
            continue
        qt = tokens(" ".join(core))
        scored = sorted(((score_probe(m, qt), m) for m in router.metas),
                        key=lambda x: -x[0])
        score, meta = (scored[0] if scored and scored[0][0] > 0
                       else (0.0, None))
        if meta is None:
            proposals.append({
                "trigger": " ".join(core),
                "probe": None,
                "verdict": "new_probe",
                "evidence": c["size"],
                "questions_sample": (c.get("questions") or [""])[0],
                "unknown_tokens": sorted(qt),
                "already_present": False,
                "score": 0.0,
            })
        else:
            vocab = meta._vocab_tokens()
            unknown = sorted(t for t in qt if t not in vocab)
            known_any = all(router._known_anywhere(t) for t in qt)
            already = any(qt <= tokens(t) for t in meta.triggers or [])
            if already:
                verdict = "covered"
            elif known_any:
                verdict = "extend_trigger"
            else:
                verdict = "new_subject"
            proposals.append({
                "probe": meta.name,
                "trigger": " ".join(core),
                "verdict": verdict,
                "evidence": c["size"],
                "questions_sample": (c.get("questions") or [""])[0],
                "unknown_tokens": unknown,
                "already_present": already,
                "score": round(score, 3),
                "runner_up": (scored[1][1].name if len(scored) > 1
                              and scored[1][0] > 0 else None),
            })
        if len(proposals) >= max_proposals:
            break
    return proposals


# ── menu gossip: publish digests, reconcile with peers ---------------------

def diff_menus(own_capabilities: Sequence[Dict[str, Any]],
               peer_capabilities: Sequence[Dict[str, Any]],
               own_name: str = "", peer_name: str = "") -> Dict[str, Any]:
    """Reconcile two published menus — the two-way gossip reconciliation.

    Publishing triggers makes vocabulary comparable across the mesh, so
    this report can say what a digest never could:

      shared      probes both sides publish (federation-safe)
      only_local  what only WE know (we are the answer for those)
      only_peer   what only the PEER knows (delegate candidates)
      drifted     same probe name, DIFFERENT trigger set — the dangerous
                  class: two instances claiming the same words with
                  different meanings will route differently by version
    """
    own = {str(c.get("name") or ""): c for c in own_capabilities or []
           if isinstance(c, dict) and c.get("name")}
    peer = {str(c.get("name") or ""): c for c in peer_capabilities or []
            if isinstance(c, dict) and c.get("name")}
    shared = sorted(set(own) & set(peer))
    drifted = []
    for name in shared:
        lt = set(own[name].get("triggers") or [])
        pt = set(peer[name].get("triggers") or [])
        if lt != pt:
            drifted.append({"probe": name,
                            "local_triggers": sorted(lt),
                            "peer_triggers": sorted(pt),
                            "added_locally": sorted(lt - pt),
                            "added_peer_side": sorted(pt - lt)})
    return {"own": own_name, "peer": peer_name,
            "shared": shared,
            "only_local": sorted(set(own) - set(peer)),
            "only_peer": sorted(set(peer) - set(own)),
            "drifted": drifted}


# ── discovery: find peers that can answer what we can't ────────────────────

@dataclass
class _Shadow:
    """A peer capability flattened to the shape score_probe reads.

    Deliberately the SAME scorer as local routing: if these tokens would
    not route a question here, they must not route it to a peer either —
    otherwise discovery recommends peers our own gate would have refused
    to trust with the same words.
    """
    name: str
    description: str
    triggers: List[str]


def _peer_probes(agent: Dict[str, Any]) -> List[_Shadow]:
    """Flatten one registry agent into shadow probes.

    Capabilities (with their triggers) win when present; bare skill names
    degrade to a one-trigger shadow (``count_incidents`` -> "count
    incidents"). Both spellings exist in the wild.
    """
    out: List[_Shadow] = []
    caps = agent.get("capabilities")
    if isinstance(caps, list):
        for c in caps:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            trig = [str(t) for t in (c.get("triggers") or [])
                    if str(t).strip()]
            out.append(_Shadow(name, str(c.get("description") or ""),
                               trig or [name.replace("_", " ")]))
    skills = agent.get("skills") or []
    seen = {s.name for s in out}
    for s in skills:
        if isinstance(s, dict):
            name = str(s.get("name") or "").strip()
            trig = [str(t) for t in (s.get("triggers") or []) if str(t).strip()]
            desc = str(s.get("description") or "")
        else:
            name, trig, desc = str(s).strip(), [], ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(_Shadow(name, desc,
                           trig or [name.replace("_", " ")]))
    return out


def discover_peers(transport: SynapseHTTP, question: str,
                   own_name: str = "", limit: int = 5,
                   preflight: bool = False,
                   min_score: float = 0.0) -> List[Dict[str, Any]]:
    """Find mesh agents whose PUBLISHED MENU matches the question.

    Ranks every peer's capabilities with ``score_probe`` — the exact
    function that routes questions locally — over DOMAIN tokens only
    (GENERIC_INTENT subtracted first, digits dropped). A peer that matches
    only on "how many" is a peer that matches on nothing.

    ``preflight=True`` additionally asks each shortlisted peer
    would-you-answer and keeps only peers whose gate says yes — discovery
    becomes "who will actually answer", not "who merely claims to".

    Returns at most ``limit`` entries, best first:
      {agent_id, matched, skills, probe, score, would?, refusal_reason?,
       menu_version?, gate_version?}
    """
    qt = tokens(question)
    domain = {t for t in qt if not t.isdigit()} - GENERIC_INTENT
    if not domain:
        return []
    agents = transport.discover(skill="")
    ranked: List[Dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_id = agent.get("agent_id") or agent.get("id") or ""
        if not agent_id or agent_id == own_name:
            continue
        probes = _peer_probes(agent)
        best = None
        for sp in probes:
            s = score_probe(sp, domain)
            if best is None or s > best[0]:
                best = (s, sp)
        if best is None or best[0] <= min_score:
            continue
        score, shadow = best
        matched = sorted(domain & set(
            tokens(" ".join([shadow.name, shadow.description,
                             " ".join(shadow.triggers)]))))
        ranked.append({"agent_id": agent_id,
                       "matched": matched,
                       "skills": agent.get("skills") or
                                 [c.name for c in probes],
                       "probe": shadow.name,
                       "score": round(score, 3),
                       "capabilities": agent.get("capabilities") or []})
    ranked.sort(key=lambda p: -p["score"])
    ranked = ranked[:max(1, limit)]
    if not preflight:
        for p in ranked:
            p["capabilities"] = p["capabilities"] or None
        return ranked
    verified: List[Dict[str, Any]] = []
    for p in ranked:
        try:
            pf = transport.preflight(p["agent_id"], question)
            p["would"] = pf.get("would")
            p["refusal_reason"] = pf.get("refusal_reason")
            p["menu_version"] = pf.get("menu_version")
            p["gate_version"] = pf.get("gate_version")
            if pf.get("would") == "answered":
                p["probe"] = pf.get("probe") or p["probe"]
                verified.append(p)
        except SynapseError as exc:
            if exc.code == "not_found":
                p["would"] = None       # peer has no preflight verb — keep unverified
                verified.append(p)
            else:
                p["would"] = "unreachable"
                p["refusal_reason"] = exc.code
    return verified


# ── delegation: refusal-aware, loop-safe, hop-limited, deadline-bounded ────

def delegate_to_peer(transport: SynapseHTTP, peer: Dict[str, Any],
                     question: str, visited: Iterable[str] = (),
                     max_hops: int = MAX_HOPS,
                     deadline: Optional[Deadline] = None,
                     deadline_ms: Optional[int] = None,
                     trust: str = "known",
                     capabilities: Optional[Iterable] = None,
                     verify: bool = True) -> Optional[Dict[str, Any]]:
    """Forward a question to a peer agent — SAFELY.

    Five invariants, each born of a way delegation used to lie:

    1. A peer REFUSAL is surfaced as a refusal. Relabelling it as an answer
       turned "I don't know" into "no response" and let callers retry
       forever; now the status says refused and the reason travels.
    2. A question never returns to an agent that already saw it (``visited``)
       — A asking B asking A is the loop that ate the afternoon.
    3. The chain is hop-limited (``max_hops``).
    4. The chain is deadline-bounded: pass the Deadline along, and a hop
       that would outlive the user's patience is cut before it starts.
    5. The peer's answer is verified against what the peer ADVERTISED
       (``verify_peer_answer``) and the verdict is attached as
       ``verified``/``verify_reason`` — the response is never silently
       trusted.

    The raw peer response is always carried under ``response`` unchanged;
    everything else is neuralosd's annotation. ``source`` stays
    ``synapse_peer`` so downstream consumers can tell a delegated answer
    from a local one. Trust classes that deny delegation refuse BEFORE any
    network call.
    """
    agent_id = peer.get("agent_id") if isinstance(peer, dict) else None
    if not agent_id:
        return None
    policy = TrustPolicy.for_level(trust)
    seen = [str(v) for v in (visited or ()) if str(v)]
    hops = len(seen)

    def _result(status, **extra):
        out = {"source": "synapse_peer", "peer": agent_id,
               "status": status, "response": None, "verified": False,
               "hop": hops + 1, "visited": seen + [agent_id],
               "max_hops": max_hops, "trust": policy.level}
        out.update(extra)
        return out

    if not policy.allow_delegation:
        return _result("delegation_denied",
                       error=f"trust class {policy.level!r} denies delegation")
    if agent_id in seen:
        return _result("loop_detected",
                       error=f"{agent_id} already handled this question "
                             f"(visited: {', '.join(seen + [agent_id])})")
    if max_hops is not None and hops >= int(max_hops):
        return _result("hop_limit",
                       error=f"hop budget exhausted ({hops}/{max_hops})")
    if deadline is None and deadline_ms is not None:
        deadline = Deadline.from_ms(deadline_ms)
    if deadline is not None and deadline.expired():
        return _result("deadline_exceeded",
                       error=f"deadline expired before contacting {agent_id} "
                             f"({deadline.remaining_ms()}ms left)")
    try:
        timeout = None
        if deadline is not None:
            timeout = deadline.remaining_s()
        result = transport.request(agent_id, question, timeout=timeout)
    except SynapseError as exc:
        return _result("delegation_failed", error=str(exc),
                       error_code=exc.code)
    except TypeError:
        # A custom transport whose request() predates the timeout kwarg.
        result = transport.request(agent_id, question)

    status, refusal_reason = _classify_reply(result)
    check = None
    if verify:
        caps = capabilities if capabilities is not None \
            else peer.get("capabilities")
        check = verify_peer_answer(result, capabilities=caps)
        verified = check["valid"]
        verify_reason = "; ".join(check["reasons"]) or "ok"
    else:
        verified, verify_reason = False, "not_verified"
    out = _result(status, response=result, verified=verified,
                  verify_reason=verify_reason,
                  verification=check,
                  refusal_reason=refusal_reason)
    return out


def _classify_reply(response: Any) -> (str, Optional[str]):
    """Map a raw peer envelope to (status, refusal_reason).

    Honest-by-default: an unrecognised shape is ``answered_unverified``
    (backward compatibility with bare bridges), never ``answered`` — the
    verifier and the caller decide what to trust, the classifier only
    refuses to LIE about refusals.
    """
    if not isinstance(response, dict):
        return "bad_reply", None
    status = str(response.get("status") or "").lower()
    reason = response.get("refusal_reason")
    if reason is not None or status in ("refused", "no_probe_matches",
                                        "low_coverage", "dropped_filter",
                                        "action_intent"):
        return "refused", (reason if isinstance(reason, str) and reason
                           else (status if status else "peer_refused"))
    if status in ("no_response", "unavailable", "timeout"):
        return status, None
    if status in ("error", "probe_error", "incompatible"):
        return status, None
    if status == "answered":
        return "answered", None
    if status in ("ok", "success", ""):
        return "answered_unverified", None
    return "answered_unverified", None
