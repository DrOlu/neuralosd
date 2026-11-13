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

Refusals are RESULTS, not errors: ``isError`` stays false, ``results`` is
null, and the envelope carries a machine-readable ``refusal`` object
(``code``, ``retryable``, ``suggested_action``, ``candidates``).

stdio JSON-RPC; stdout carries protocol frames only. A probe (or the router's
possessive-normalisation notice) may print, so anything the instance writes
is redirected to stderr while a tool runs.

usage: python3 -m neuralosd.mcp --dir /opt/chinook
       (or python3 -m neuralosd.mcp /opt/chinook)
"""
import collections
import contextlib
import hashlib
import json
import os
import sys

from . import __version__
from .router import GATE_VERSION, NoResults, audit_fields, audit_source

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "neuralosd"

# Loaded once per process: re-executing probes.py on every tools/list was a
# side effect on a read-only call (and made the server unusable for a probe
# that takes seconds to import).
_INSTANCES = {}

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": False}

# A refusal an agent can branch on without parsing prose. `action_intent` is
# the only hard no: the runtime answers questions, it does not act.
_HARD = frozenset({"action_intent"})

# Idempotency memory: an agent that retries after a timeout must get the
# first answer back, not a second execution. Bounded — this is a retry
# window, not a second cache.
_IDEM = collections.OrderedDict()
_IDEM_MAX = 500

_SUGGEST = {
    "no_probe_matches":
        "Call search() to see which questions this instance can answer, "
        "then rephrase using one of its triggers.",
    "ambiguous":
        "Make the question more specific; describe() the candidates to see "
        "which arguments disambiguate them.",
    "args_not_extractable":
        "Call describe(probe) and name the required argument explicitly in "
        "the question.",
    "out_of_range":
        "Correct the argument value; describe(probe) lists the allowed range.",
    "action_intent":
        "This runtime answers questions; it does not perform actions. Route "
        "the action through the actuation surface with human approval.",
    "no_results":
        "The probe ran but produced no rows; loosen the filter or check that "
        "the data source has the rows you expect.",
    "dropped_filter":
        "The answer would have ignored a filter you named; rephrase, or ask "
        "for the unfiltered total explicitly.",
    "low_coverage":
        "Rephrase with the exact trigger wording; catalog() lists the "
        "triggers.",
    "below_floor":
        "The match was below the calibrated confidence floor; ask a more "
        "specific question.",
    "comparative":
        "Comparisons are not routable; ask for each value separately.",
    "model_abstained":
        "No available probe answers this; catalog() lists what is covered.",
}


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
        _INSTANCES[key] = inst
    return inst


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
                    "question": {"type": "string",
                                 "description": "The question to answer."},
                    "agent_id": {"type": "string",
                                 "description": "Who is asking (audit stamp)."},
                    "session_id": {"type": "string",
                                   "description": "The agent session (audit "
                                                  "stamp)."},
                    "run_id": {"type": "string",
                               "description": "The run/episode this call "
                                              "belongs to (audit stamp)."},
                    "idempotency_key": {
                        "type": "string",
                        "description": "Retry-safe key: a replay with the "
                                       "same key returns the first result "
                                       "without executing again."},
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
                "Rank probes against a query WITHOUT executing one. Call this "
                "before ask() to see which tools could answer, with scores and "
                "trigger examples."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50,
                              "default": 8},
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
                "Return a probe's description, triggers, argument schema, PII "
                "fields, execution tier and whether it needs confirmation."),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Describe one probe", **_READ_ONLY},
        },
        {
            "name": "catalog",
            "title": "What can this instance answer?",
            "description": "The instance's catalogue of questions, as Markdown.",
            "inputSchema": {"type": "object", "properties": {},
                            "additionalProperties": False},
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
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            "annotations": {"title": "Dry-run a question", **_READ_ONLY},
        },
        {
            "name": "health",
            "title": "Instance health",
            "description": "Liveness, instance name, probe count, menu and "
                           "gate versions.",
            "inputSchema": {"type": "object", "properties": {},
                            "additionalProperties": False},
            "annotations": {"title": "Instance health", **_READ_ONLY},
        },
    ]


def _refusal_contract(env):
    code = env.get("refusal_reason") or "no_probe_matches"
    return {
        "code": code,
        "retryable": code not in _HARD,
        "suggested_action": _SUGGEST.get(
            code, "Call catalog() to see what this instance covers."),
        "candidates": env.get("scores") or [],
        "missing": env.get("discarded") or None,
    }


def _tool_version(entry):
    """A stable version for one tool contract (name, description, schema,
    triggers). An agent's prompt is pinned to this; a menu change that
    alters it is visible instead of silent."""
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
        ck = ((agent_id or "", session_id or "", idem_key)
              if idem_key else None)
        if ck is not None and ck in _IDEM:
            env = dict(_IDEM[ck])
            env["_meta"] = {"instance": inst.name, "agent_id": agent_id,
                            "session_id": session_id, "run_id": run_id,
                            "menu_version": inst.router.menu_version,
                            "replayed": True}
            return env, False, None

        with _quiet(), audit_source("mcp"), audit_fields(
                agent_id=agent_id, session_id=session_id, run_id=run_id,
                surface="mcp"):
            try:
                env = inst.ask(question)
            except NoResults as exc:
                env = exc.envelope
        env = dict(env)
        env["_meta"] = {"instance": inst.name, "agent_id": agent_id,
                        "session_id": session_id, "run_id": run_id,
                        "menu_version": inst.router.menu_version}
        # Results are DATA: the harness must never execute instructions that
        # appear inside a row. The provenance says where the bytes came from.
        env["content_trust"] = "untrusted"
        env["provenance"] = {"instance": inst.name,
                             "probe": env.get("probe"),
                             "ask_id": env.get("ask_id"),
                             "ts": env.get("ts"),
                             "run_id": run_id,
                             "menu_version": inst.router.menu_version}
        if env.get("mode") == "refused":
            env["refusal"] = _refusal_contract(env)
        if ck is not None:
            _IDEM[ck] = dict(env)
            _IDEM.move_to_end(ck)
            while len(_IDEM) > _IDEM_MAX:
                _IDEM.popitem(last=False)
        return env, str(env.get("mode") or "").endswith("-error"), None

    if name == "search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search: 'query' must be a non-empty string")
        k = _int(arguments.get("top_k"), 8, 1, 50)
        ranked = inst.router.retrieve(query, k)
        return {"query": query,
                "menu_version": inst.router.menu_version,
                "results": [{"name": m.name,
                             "description": m.description,
                             "score": round(score, 3),
                             "triggers": list(m.triggers)[:5]}
                            for m, score in ranked]}, False, None

    if name == "describe":
        target = arguments.get("name")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("describe: 'name' must be a non-empty string")
        target = target.strip()
        entry = next((m for m in inst.menu if m["name"] == target), None)
        meta = next((m for m in inst.router.metas if m.name == target), None)
        if entry is None or meta is None:
            raise KeyError(f"unknown probe: {target}")
        return {"name": meta.name,
                "description": meta.description,
                "triggers": list(meta.triggers),
                "parameters": entry["parameters"],
                "pii": list(meta.pii or []),
                "tier": getattr(meta, "tier", None),
                "confirm": bool(getattr(meta, "confirm", False)),
                "tool_version": _tool_version(entry),
                "menu_version": inst.router.menu_version}, False, None

    if name == "catalog":
        text = _catalog_text(inst)
        return ({"catalog": text, "menu_version": inst.router.menu_version},
                False, text)

    if name == "explain":
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("explain: 'question' must be a non-empty string")
        k = _int(arguments.get("top_k"), 8, 1, 50)
        return inst.router.dry_run(question, k), False, None

    if name == "health":
        return {"ok": True, "instance": inst.name,
                "probes": len(inst.router.metas),
                "menu_version": inst.router.menu_version,
                "gate_version": GATE_VERSION}, False, None

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
            payload, is_error, text = _call(
                _instance(instance_dir), name, arguments)
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
