"""MCP server — the guarded agent surface.

Every tool call goes through the SAME router that serves ``neuralosd ask``:
normalisation, retrieval, the calibrated gate, argument caging, PII masking,
the results gate, the cache and the audit ledger. An agent calling MCP gets
exactly the guarantees the CLI gets — no second, unguarded execution path.

Tools
-----
    ask       route a question and return the full envelope
    search    rank the probes for a query (progressive disclosure)
    describe  one probe: schema, triggers, PII fields, tier
    catalog   what this instance can answer (Markdown)
    explain   dry-run: what would be chosen, without executing
    health    liveness + menu version
    memory    per-session remember / recall / last / turns
    trace     the run's tool-call episode (read / runs)
    delegate  forward a question to a mesh peer (A2A)
    discover  rank mesh peers that could answer a question
    build     natural language in, verified capability out (pi)
    shadow    report / evaluate / promote a shadowed metric
    mothball  demote probes nobody asks about
    confirm   complete a pending HITL confirmation

Refusals are RESULTS, not errors: ``isError`` stays false, ``results`` is
null, and the envelope carries a machine-readable ``refusal`` object
(``code``, ``retryable``, ``suggested_action``, ``candidates``, ``missing``).
A probe with ``confirm=True`` pauses: the result is
``mode: pending_confirmation`` with a token for ``confirm``.

stdio JSON-RPC; stdout carries protocol frames only. A probe (or the router's
possessive-normalisation notice) may print, so anything the instance writes
is redirected to stderr while a tool runs.

usage: python3 -m neuralosd.mcp --dir /opt/chinook
       (or python3 -m neuralosd.mcp /opt/chinook)
"""
import contextlib
import hashlib
import json
import os
import sys

from . import __version__
from .agentic import (BudgetExceeded, BudgetStore, IdempotencyStore,
                      PolicyDenied, SessionStore, TraceStore,
                      refusal_contract, resolve_agent)
from .hitl import ConfirmRequired, ConfirmStore
from .router import (GATE_VERSION, NoResults, audit_fields, audit_source,
                     pii_policy)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "neuralosd"

# Loaded once per process: re-executing probes.py on every tools/list was a
# side effect on a read-only call (and made the server unusable for a probe
# that takes seconds to import).
_INSTANCES = {}
_ROLE_INSTANCES = {}
_SESSIONS = {}
_TRACES = {}
_CONFIRM = {}
_IDEM = IdempotencyStore()
_BUDGETS = BudgetStore()

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": False}


@contextlib.contextmanager
def _quiet():
    """Keep the protocol stream clean: instance prints go to stderr."""
    old = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = old


def _instance(instance_dir):
    key = os.path.abspath(os.path.expanduser(instance_dir))
    inst = _INSTANCES.get(key)
    if inst is None:
        from ._cmd._common import load_instance
        with _quiet():
            inst = load_instance(key)
        inst.router.confirm_store = _confirm_store(key)
        _INSTANCES[key] = inst
    return inst


def _confirm_store(key):
    store = _CONFIRM.get(key)
    if store is None:
        store = ConfirmStore()
        _CONFIRM[key] = store
    return store


def _sessions(inst):
    key = os.path.abspath(inst.state_dir)
    store = _SESSIONS.get(key)
    if store is None:
        store = SessionStore(key)
        _SESSIONS[key] = store
    return store


def _traces(inst):
    key = os.path.abspath(inst.state_dir)
    store = _TRACES.get(key)
    if store is None:
        store = TraceStore(key)
        _TRACES[key] = store
    return store


def _role_instance(inst, role):
    """The role-scoped view of the same instance (acl.json roles)."""
    if not role:
        return inst
    key = (os.path.abspath(inst.state_dir), role)
    target = _ROLE_INSTANCES.get(key)
    if target is None:
        from ._cmd._common import load_instance
        with _quiet():
            target = load_instance(key[0], role=role)
        target.router.confirm_store = _confirm_store(key[0])
        _ROLE_INSTANCES[key] = target
    return target


def _policy(inst, agent_id, charge=False):
    """Resolve the agent's allow-list/role/PII/budget; deny by default when
    acl.json declares an ``agents`` block and the agent is not in it."""
    policy = resolve_agent(inst.state_dir, agent_id)
    if not policy["allowed"]:
        raise PolicyDenied(
            f"agent {agent_id!r} is not allowed on this instance "
            f"(add it to acl.json agents)")
    if charge and agent_id and policy["budget"]["calls"] > 0:
        retry = _BUDGETS.check(agent_id, policy["budget"]["calls"],
                               policy["budget"]["window"])
        if retry is not None:
            raise BudgetExceeded(retry, policy["budget"]["calls"],
                                 policy["budget"]["window"])
    return policy


def _text(value, limit=200):
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def _int(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _tools():
    """The fixed agent surface. Probes are reached through ask/search/describe
    rather than dumped into the context: a 200-probe menu is a window killer."""
    return [
        {
            "name": "ask",
            "title": "Ask the instance",
            "description": (
                "Route a question through the verified pipeline: retrieve, "
                "gate, extract caged arguments, execute, mask PII, audit. "
                "Refusals are results (isError=false, results=null, "
                "refusal.code) — never guesses."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "idempotency_key": {
                        "type": "string",
                        "description": "A replay with the same key returns "
                                       "the first result without executing."},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Ask the instance",
                            "readOnlyHint": False,
                            "destructiveHint": False,
                            "idempotentHint": False,
                            "openWorldHint": False},
        },
        {
            "name": "search",
            "title": "Find tools for a question",
            "description": (
                "Rank probes against a query WITHOUT executing one."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50,
                              "default": 8},
                    "agent_id": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Find tools for a question", **_READ_ONLY},
        },
        {
            "name": "describe",
            "title": "Describe one probe",
            "description": (
                "A probe's description, triggers, argument schema, PII fields, "
                "tier, confirmation flag and tool_version."),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"},
                               "agent_id": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Describe one probe", **_READ_ONLY},
        },
        {
            "name": "catalog",
            "title": "What can this instance answer?",
            "description": "The instance's catalogue of questions, as Markdown.",
            "inputSchema": {
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "additionalProperties": False,
            },
            "annotations": {"title": "What can this instance answer?",
                            **_READ_ONLY},
        },
        {
            "name": "explain",
            "title": "Dry-run a question",
            "description": (
                "Run the decision half of ask() WITHOUT executing a probe or "
                "writing an audit row: would it answer, which probe, which "
                "arguments, why not."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50,
                              "default": 8},
                    "agent_id": {"type": "string"},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Dry-run a question", **_READ_ONLY},
        },
        {
            "name": "health",
            "title": "Instance health",
            "description": "Liveness, probe count, menu and gate versions.",
            "inputSchema": {"type": "object", "properties": {},
                            "additionalProperties": False},
            "annotations": {"title": "Instance health", **_READ_ONLY},
        },
        {
            "name": "memory",
            "title": "Session memory",
            "description": (
                "remember a fact for a session, recall the closest turns, "
                "read the last turn, or count turns."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                           "enum": ["remember", "recall", "last", "turns"]},
                    "session_id": {"type": "string"},
                    "text": {"type": "string"},
                    "query": {"type": "string"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 50,
                          "default": 5},
                    "agent_id": {"type": "string"},
                },
                "required": ["op", "session_id"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Session memory", **_READ_ONLY},
        },
        {
            "name": "trace",
            "title": "Run trace",
            "description": "Read a run's tool-call episode, or list runs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["read", "runs"]},
                    "run_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1,
                              "maximum": 1000},
                    "agent_id": {"type": "string"},
                },
                "required": ["op"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Run trace", **_READ_ONLY},
        },
        {
            "name": "delegate",
            "title": "Delegate to a mesh peer",
            "description": (
                "Forward a question to a peer agent (A2A): discovery, "
                "preflight, trust policy, verification and loop limits."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "peer": {"type": "string"},
                    "question": {"type": "string"},
                    "gateway": {"type": "string"},
                    "trust": {"type": "string", "default": "known"},
                    "agent_id": {"type": "string"},
                },
                "required": ["peer", "question"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Delegate to a mesh peer",
                            "readOnlyHint": False,
                            "destructiveHint": False,
                            "idempotentHint": False,
                            "openWorldHint": True},
        },
        {
            "name": "discover",
            "title": "Discover mesh peers",
            "description": "Rank mesh peers whose published menu matches a "
                           "question.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "gateway": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                              "default": 5},
                    "agent_id": {"type": "string"},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Discover mesh peers",
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                            "openWorldHint": True},
        },
        {
            "name": "build",
            "title": "Build a capability",
            "description": (
                "Natural language in, verified capability out: pi writes the "
                "probe, neuralosd judges it (banks untouched, instance loads, "
                "both banks green) and restores everything on failure."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "model": {"type": "string"},
                    "provider": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 30,
                                "maximum": 3600, "default": 900},
                    "agent_id": {"type": "string"},
                },
                "required": ["description"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Build a capability",
                            "readOnlyHint": False,
                            "destructiveHint": True,
                            "idempotentHint": False,
                            "openWorldHint": True},
        },
        {
            "name": "shadow",
            "title": "Shadow a generated probe",
            "description": "Report, evaluate or promote shadowed metrics. A "
                           "shadowed probe is NOT routable until promotion.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {"type": "string",
                           "enum": ["report", "evaluate", "promote"]},
                    "name": {"type": "string"},
                    "min_agreement": {"type": "number"},
                    "min_runs": {"type": "integer"},
                    "max_age_days": {"type": "number"},
                    "agent_id": {"type": "string"},
                },
                "required": ["op"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Shadow a generated probe",
                            "readOnlyHint": False,
                            "destructiveHint": True,
                            "idempotentHint": False,
                            "openWorldHint": False},
        },
        {
            "name": "mothball",
            "title": "Mothball stale probes",
            "description": "Demote probes nobody asked about (dry-run unless "
                           "apply=true); at least one probe stays routable.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "age_days": {"type": "number", "default": 180},
                    "apply": {"type": "boolean", "default": False},
                    "agent_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "annotations": {"title": "Mothball stale probes",
                            "readOnlyHint": False,
                            "destructiveHint": True,
                            "idempotentHint": False,
                            "openWorldHint": False},
        },
        {
            "name": "confirm",
            "title": "Confirm a pending action",
            "description": "Complete a probe that paused for human approval; "
                           "the token comes from an ask() result with mode="
                           "pending_confirmation.",
            "inputSchema": {
                "type": "object",
                "properties": {"token": {"type": "string"},
                               "agent_id": {"type": "string"}},
                "required": ["token"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Confirm a pending action",
                            "readOnlyHint": False,
                            "destructiveHint": True,
                            "idempotentHint": False,
                            "openWorldHint": False},
        },
    ]


def tools():
    """The tool list, for surfaces that serve it over HTTP."""
    return _tools()


def _tool_version(entry):
    blob = json.dumps(entry, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def _catalog_text(inst):
    path = os.path.join(getattr(inst, "state_dir", "") or "", "CATALOG.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        pass
    lines = [f"# {inst.name}", "",
             f"{len(inst.router.metas)} probe(s) in menu "
             f"{inst.router.menu_version}", ""]
    for m in inst.router.metas:
        lines.append(f"- **{m.name}** — {m.description}")
        if m.triggers:
            lines.append(f"  - e.g. {m.triggers[0]!r}")
    return "\n".join(lines) + "\n"


def _mesh_transport(gateway=None):
    from .synapse import SynapseHTTP
    gw = gateway or os.environ.get("REACTORPRO_GATEWAY_URL",
                                   "http://127.0.0.1:3000")
    return SynapseHTTP(gateway=gw)


def _build_capability(description, instance_dir, model=None, provider=None,
                      timeout=900):
    from .pisources import build_capability
    return build_capability(description, instance_dir=instance_dir,
                            model=model or os.environ.get(
                                "NEURALOSD_PI_MODEL") or "z-ai/glm-5.3-flash",
                            provider=provider or os.environ.get(
                                "NEURALOSD_PI_PROVIDER") or "openrouter",
                            timeout=timeout)


def _trace_event(inst, run_id, name, agent_id, session_id, payload):
    if not run_id:
        return
    event = {"tool": name, "agent_id": agent_id,
             "session_id": session_id}
    if isinstance(payload, dict):
        for key in ("probe", "mode", "ask_id", "refusal_reason", "would",
                    "would_answer", "status", "peer", "ok"):
            if key in payload:
                event[key] = payload[key]
    _traces(inst).record(run_id, event)


def _call(inst, name, arguments):
    """Return (payload, is_error, text). Refusals are not errors."""
    if not isinstance(arguments, dict):
        raise TypeError("arguments must be a JSON object")

    if name == "ask":
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("ask: 'question' must be a non-empty string")
        agent_id = _text(arguments.get("agent_id"))
        session_id = _text(arguments.get("session_id"))
        run_id = _text(arguments.get("run_id"))
        idem_key = _text(arguments.get("idempotency_key"))
        policy = _policy(inst, agent_id)
        target = _role_instance(inst, policy["role"])
        ck = ((agent_id or "", session_id or "", idem_key)
              if idem_key else None)
        if ck is not None and _IDEM.get(ck) is not None:
            env = dict(_IDEM.get(ck))
            env["_meta"] = {"instance": target.name, "agent_id": agent_id,
                            "session_id": session_id, "run_id": run_id,
                            "menu_version": target.router.menu_version,
                            "replayed": True}
            _trace_event(inst, run_id or env.get("ask_id"), "ask", agent_id,
                         session_id, {**env, "mode": "replayed"})
            return env, False, None

        _policy(inst, agent_id, charge=True)      # budget: one ask, one call
        sessions = _sessions(inst)
        previous = sessions.last(session_id) if session_id else None
        with _quiet(), audit_source("mcp"), audit_fields(
                agent_id=agent_id, session_id=session_id, run_id=run_id,
                surface="mcp"), pii_policy(policy["pii"]):
            try:
                env = target.ask(question)
            except NoResults as exc:
                env = dict(exc.envelope)
                env["mode"] = "refused"
            except ConfirmRequired as exc:
                pending = {"mode": "pending_confirmation",
                           "confirm_token": exc.token,
                           "probe": exc.probe_name,
                           "reason": exc.reason,
                           "expires_in": _confirm_store(
                               os.path.abspath(inst.state_dir)).ttl}
                _trace_event(inst, run_id, "ask", agent_id, session_id,
                             pending)
                return pending, False, None
        env = dict(env)
        env["_meta"] = {"instance": target.name, "agent_id": agent_id,
                        "session_id": session_id, "run_id": run_id,
                        "menu_version": target.router.menu_version}
        env["content_trust"] = "untrusted"
        env["provenance"] = {"instance": target.name,
                             "probe": env.get("probe"),
                             "ask_id": env.get("ask_id"),
                             "ts": env.get("ts"),
                             "run_id": run_id,
                             "menu_version": target.router.menu_version}
        if env.get("mode") == "refused":
            env["refusal"] = refusal_contract(env)
        if session_id:
            sessions.remember(
                session_id, question,
                {"probe": env.get("probe"), "mode": env.get("mode"),
                 "ask_id": env.get("ask_id"), "agent_id": agent_id})
            env["_meta"]["session"] = {
                "turn": sessions.turns(session_id),
                "previous": (previous or {}).get("text")}
        if ck is not None:
            _IDEM.put(ck, dict(env))
        _trace_event(inst, run_id or env.get("ask_id"), "ask", agent_id,
                     session_id, env)
        return env, str(env.get("mode") or "").endswith("-error"), None

    if name == "search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search: 'query' must be a non-empty string")
        agent_id = _text(arguments.get("agent_id"))
        policy = _policy(inst, agent_id)
        target = _role_instance(inst, policy["role"])
        k = _int(arguments.get("top_k"), 8, 1, 50)
        ranked = target.router.retrieve(query, k)
        return {"query": query,
                "menu_version": target.router.menu_version,
                "results": [{"name": m.name,
                             "description": m.description,
                             "score": round(score, 3),
                             "triggers": list(m.triggers)[:5]}
                            for m, score in ranked]}, False, None

    if name == "describe":
        target_name = arguments.get("name")
        if not isinstance(target_name, str) or not target_name.strip():
            raise ValueError("describe: 'name' must be a non-empty string")
        agent_id = _text(arguments.get("agent_id"))
        policy = _policy(inst, agent_id)
        target = _role_instance(inst, policy["role"])
        target_name = target_name.strip()
        entry = next((m for m in target.menu if m["name"] == target_name),
                     None)
        meta = next((m for m in target.router.metas
                     if m.name == target_name), None)
        if entry is None or meta is None:
            raise KeyError(f"unknown probe: {target_name}")
        return {"name": meta.name,
                "description": meta.description,
                "triggers": list(meta.triggers),
                "parameters": entry["parameters"],
                "pii": list(meta.pii or []),
                "tier": getattr(meta, "tier", None),
                "confirm": bool(getattr(meta, "confirm", False)),
                "tool_version": _tool_version(entry),
                "menu_version": target.router.menu_version}, False, None

    if name == "catalog":
        agent_id = _text(arguments.get("agent_id"))
        policy = _policy(inst, agent_id)
        target = _role_instance(inst, policy["role"])
        text = _catalog_text(target)
        return ({"catalog": text, "menu_version": target.router.menu_version},
                False, text)

    if name == "explain":
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("explain: 'question' must be a non-empty string")
        agent_id = _text(arguments.get("agent_id"))
        policy = _policy(inst, agent_id)
        target = _role_instance(inst, policy["role"])
        k = _int(arguments.get("top_k"), 8, 1, 50)
        return target.router.dry_run(question, k), False, None

    if name == "health":
        return {"ok": True, "instance": inst.name,
                "probes": len(inst.router.metas),
                "menu_version": inst.router.menu_version,
                "gate_version": GATE_VERSION}, False, None

    if name == "memory":
        session_id = _text(arguments.get("session_id"))
        if not session_id:
            raise ValueError("memory: 'session_id' is required")
        op = arguments.get("op")
        store = _sessions(inst)
        if op == "remember":
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("memory remember: 'text' is required")
            return store.remember(session_id, text), False, None
        if op == "recall":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("memory recall: 'query' is required")
            k = _int(arguments.get("k"), 5, 1, 50)
            return {"results": store.recall(session_id, query, k=k)}, False, None
        if op == "last":
            return {"last": store.last(session_id)}, False, None
        if op == "turns":
            return {"turns": store.turns(session_id)}, False, None
        raise ValueError("memory: 'op' must be remember, recall, last or turns")

    if name == "trace":
        op = arguments.get("op")
        store = _traces(inst)
        if op == "runs":
            return {"runs": store.runs()}, False, None
        if op == "read":
            run_id = _text(arguments.get("run_id"))
            if not run_id:
                raise ValueError("trace read: 'run_id' is required")
            limit = _int(arguments.get("limit"), 0, 0, 1000) or None
            return ({"run_id": run_id, "events": store.read(run_id, limit)},
                    False, None)
        raise ValueError("trace: 'op' must be read or runs")

    if name == "delegate":
        peer_id = _text(arguments.get("peer"))
        question = arguments.get("question")
        if not peer_id or not isinstance(question, str) or not question.strip():
            raise ValueError("delegate: 'peer' and 'question' are required")
        from .synapse import delegate_to_peer, discover_peers
        transport = _mesh_transport(_text(arguments.get("gateway")))
        peers = discover_peers(transport, question)
        peer = next((p for p in peers
                     if p.get("agent_id") == peer_id), None)
        if peer is None:
            raise KeyError(f"peer not found or does not match: {peer_id}")
        result = delegate_to_peer(
            transport, peer, question,
            trust=_text(arguments.get("trust")) or "known")
        return result or {"error": "delegation returned no result"}, \
            result is None, None

    if name == "discover":
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("discover: 'question' is required")
        from .synapse import discover_peers
        transport = _mesh_transport(_text(arguments.get("gateway")))
        limit = _int(arguments.get("limit"), 5, 1, 20)
        return ({"peers": discover_peers(transport, question, limit=limit)},
                False, None)

    if name == "build":
        description = arguments.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("build: 'description' is required")
        _policy(inst, _text(arguments.get("agent_id")))
        timeout = _int(arguments.get("timeout"), 900, 30, 3600)
        report = _build_capability(
            description.strip()[:4000], inst.state_dir,
            model=_text(arguments.get("model")),
            provider=_text(arguments.get("provider")), timeout=timeout)
        # The menu changed; drop the cached instance so the next call reloads.
        _INSTANCES.pop(os.path.abspath(inst.state_dir), None)
        _ROLE_INSTANCES.clear()
        return report, False, None

    if name == "shadow":
        from . import loop
        op = arguments.get("op")
        if op == "report":
            return loop.shadow_report(inst.state_dir), False, None
        metric = _text(arguments.get("name"))
        if not metric:
            raise ValueError("shadow: 'name' is required for evaluate/promote")
        try:
            if op == "evaluate":
                return loop.shadow_evaluate(inst.state_dir, metric), False, None
            if op == "promote":
                promoted = loop.shadow_promote(
                    inst.state_dir, metric,
                    min_agreement=float(arguments.get("min_agreement", 0.9)),
                    min_runs=_int(arguments.get("min_runs"), 3, 1, 1000),
                    max_age_days=float(arguments.get("max_age_days", 14)))
                return {"promoted": promoted, "name": metric}, False, None
        except SystemExit as exc:
            raise ValueError(str(exc))
        raise ValueError("shadow: 'op' must be report, evaluate or promote")

    if name == "mothball":
        from . import loop
        age_days = float(arguments.get("age_days", 180) or 180)
        apply = bool(arguments.get("apply", False))
        try:
            return loop.mothball(inst.state_dir, age_days=age_days,
                                 apply=apply), False, None
        except SystemExit as exc:
            raise ValueError(str(exc))

    if name == "confirm":
        token = _text(arguments.get("token"), 200)
        if not token:
            raise ValueError("confirm: 'token' is required")
        result = _confirm_store(os.path.abspath(inst.state_dir)).confirm(token)
        if isinstance(result, dict) and result.get("error"):
            return result, True, None
        result = dict(result) if isinstance(result, dict) else {"result": result}
        result["content_trust"] = "untrusted"
        return result, False, None

    raise KeyError(f"unknown tool: {name}")


def _tool_result(payload, is_error=False, text=None):
    if text is None:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": bool(is_error)}


def handle(request, instance_dir: str):
    """Handle one JSON-RPC request. Returns None for notifications."""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = request.get("method")
    req_id = request.get("id")

    # MCP notifications carry no id and must not be answered.
    if req_id is None and isinstance(method, str):
        return None

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": SERVER_NAME,
                                          "version": __version__}}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"tools": _tools()}}

    if method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602,
                              "message": "params must be an object"}}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            target = _instance(instance_dir)
            # Identity is required by every data/tool call once acl.json
            # declares an agents block; health stays open like /healthz.
            if name != "health":
                _policy(target, _text(
                    arguments.get("agent_id")
                    if isinstance(arguments, dict) else None))
            payload, is_error, text = _call(target, name, arguments)
        except PolicyDenied as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result(
                        {"error": str(exc), "code": "policy_denied"}, True)}
        except BudgetExceeded as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result(
                        {"error": str(exc), "code": "budget_exceeded",
                         "retry_after": exc.retry_after,
                         "limit": exc.limit, "window": exc.window}, True)}
        except SystemExit as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result({"error": str(exc)}, True)}
        except Exception as exc:  # a bad tool call is a tool error, not a crash
            payload = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            is_error, text = True, payload["error"]
        return {"jsonrpc": "2.0", "id": req_id,
                "result": _tool_result(payload, is_error, text)}

    if method is None:
        return None
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    instance_dir = os.getcwd()
    if "--dir" in argv:
        i = argv.index("--dir")
        if i + 1 >= len(argv):
            print("error: --dir needs a path", file=sys.stderr)
            return 2
        instance_dir = argv[i + 1]
    elif argv:
        instance_dir = argv[0]

    try:
        _instance(instance_dir)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700,
                                  "message": f"parse error: {exc}"}}
        else:
            response = handle(request, instance_dir)
        if response:
            print(json.dumps(response, ensure_ascii=False, default=str),
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
