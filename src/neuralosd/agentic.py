"""Agent-facing contracts shared by the MCP and HTTP surfaces.

A human asks once; an agent plans, retries, delegates and runs long. These
are the pieces that difference needs and a CLI does not:

    refusal_contract   a refusal an agent can branch on without prose
    IdempotencyStore   a retry after a timeout returns the FIRST result
    BudgetStore        per-agent call budgets, so a loop cannot hammer us
    load_acl/resolve_agent  per-agent roles, PII policy and budgets
    SessionStore       per-session remember/recall + the previous turn
    TraceStore         a run/episode log spanning several tool calls
    RunStore           long-running asks: submit, poll, cancel

They live in one module so MCP and HTTP cannot drift apart. Nothing here
executes a probe; the surface does that through the router.
"""
import collections
import hashlib
import json
import os
import re
import threading
import time
import uuid

from .memory import MemoryStore, lexical_embed


# ── refusals ──────────────────────────────────────────────────────────────

# `action_intent` is the only hard no: the runtime answers questions, it
# does not act. Everything else is worth a retry with a better question.
HARD_REFUSALS = frozenset({"action_intent"})

SUGGESTIONS = {
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


def refusal_contract(env):
    """The machine-readable half of a refusal envelope."""
    code = env.get("refusal_reason") or "no_probe_matches"
    return {
        "code": code,
        "retryable": code not in HARD_REFUSALS,
        "suggested_action": SUGGESTIONS.get(
            code, "Call catalog() to see what this instance covers."),
        "candidates": env.get("scores") or [],
        "missing": env.get("discarded") or None,
    }


# ── errors ────────────────────────────────────────────────────────────────

class PolicyDenied(Exception):
    """The agent is not allowed on this instance (or this role)."""


class BudgetExceeded(Exception):
    """The agent used its call budget for the window."""

    def __init__(self, retry_after, limit, window):
        self.retry_after = max(1, int(retry_after))
        self.limit = limit
        self.window = window
        super().__init__(
            f"budget exhausted: {limit} calls / {window}s "
            f"(retry in {self.retry_after}s)")


# ── policy ────────────────────────────────────────────────────────────────

def load_acl(instance_dir):
    path = os.path.join(instance_dir, "acl.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_agent(instance_dir, agent_id):
    """Resolve an agent's policy from ``acl.json``.

    Backward compatible: no ``agents`` block means "every agent is allowed
    with the instance defaults". When the block exists it is an allow-list:
    an unlisted agent is denied (fail closed), because an ACL that silently
    admits unknown agents is not an ACL.

    Returns ``{"allowed", "role", "pii", "budget"}`` where ``budget`` is
    ``{"calls": int, "window": seconds}`` or ``{}`` for unlimited.
    """
    acl = load_acl(instance_dir)
    agents = acl.get("agents")
    default = {"allowed": True, "role": None, "pii": True,
               "budget": {"calls": 0, "window": 3600}}
    if not isinstance(agents, dict):
        return default
    if not agent_id:
        return {**default, "allowed": False}
    entry = agents.get(agent_id)
    if not isinstance(entry, dict):
        return {**default, "allowed": False}
    budget = entry.get("budget")
    if not isinstance(budget, dict):
        budget = {}
    return {
        "allowed": True,
        "role": entry.get("role"),
        "pii": bool(entry.get("pii", True)),
        "budget": {"calls": int(budget.get("calls", 0) or 0),
                   "window": int(budget.get("window", 3600) or 3600)},
    }


# ── idempotency ───────────────────────────────────────────────────────────

class IdempotencyStore:
    """Bounded replay memory: a retry with the same key gets the first
    result instead of a second execution."""

    def __init__(self, max_entries=500):
        self._items = collections.OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key, value):
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


# ── budgets ───────────────────────────────────────────────────────────────

class BudgetStore:
    """Sliding-window call budgets, per agent, in memory."""

    def __init__(self):
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def check(self, agent_id, calls, window):
        """Return None when allowed, else seconds until the window frees."""
        if not agent_id or calls <= 0:
            return None
        now = time.time()
        with self._lock:
            hits = self._hits[agent_id]
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) < calls:
                hits.append(now)
                return None
            return max(1, int(window - (now - hits[0])))


# ── sessions ──────────────────────────────────────────────────────────────

class SessionStore:
    """Per-session turn memory. Uses the existing MemoryStore with a
    deterministic lexical embedding, so it works with no model installed;
    pass ``embed_fn`` to use needle embeddings when they are available."""

    def __init__(self, instance_dir, embed_fn=None):
        state = os.path.join(instance_dir, ".agentic")
        self._memory = MemoryStore(state, embed_fn or lexical_embed)

    def remember(self, session_id, text, meta=None):
        return self._memory.remember(session_id, text, meta=meta)

    def recall(self, session_id, query, k=5):
        return self._memory.recall(session_id, query, k=k)

    def last(self, session_id):
        path = os.path.join(self._memory.dir, f"{session_id}.jsonl")
        try:
            last = None
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last = line
            return json.loads(last) if last else None
        except (OSError, ValueError):
            return None

    def turns(self, session_id):
        path = os.path.join(self._memory.dir, f"{session_id}.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0


# ── tracing ───────────────────────────────────────────────────────────────

class TraceStore:
    """A run/episode log: one JSONL file per run, bounded per run."""

    def __init__(self, instance_dir, max_events=1000):
        self.dir = os.path.join(instance_dir, ".agentic", "traces")
        self.max_events = max_events
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, run_id):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id))[:80] or "run"
        return os.path.join(self.dir, f"{safe}.jsonl")

    def record(self, run_id, event):
        if not run_id:
            return
        event = {"ts": time.time(), **event}
        path = self._path(run_id)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str)
                         + "\n")
        except OSError:
            pass

    def read(self, run_id, limit=None):
        path = self._path(run_id)
        try:
            with open(path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            return []
        return rows[-limit:] if limit else rows

    def runs(self):
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return []
        return [n[:-6] for n in names if n.endswith(".jsonl")]


# ── long-running runs ─────────────────────────────────────────────────────

class RunStore:
    """Submit a long ask, poll it, cancel it. In-memory and bounded: a run
    that outlives the process was never durable (use Temporal for that)."""

    def __init__(self, max_runs=200):
        self._runs = collections.OrderedDict()
        self._lock = threading.Lock()
        self._max = max_runs

    def submit(self, fn, meta=None):
        run_id = uuid.uuid4().hex[:12]
        run = {"run_id": run_id, "status": "queued", "created": time.time(),
               "started": None, "finished": None, "result": None,
               "error": None, "cancel": False, "meta": meta or {}}
        with self._lock:
            self._runs[run_id] = run
            while len(self._runs) > self._max:
                self._runs.popitem(last=False)

        def _work():
            run["status"] = "running"
            run["started"] = time.time()
            try:
                if run["cancel"]:
                    run["status"] = "cancelled"
                else:
                    run["result"] = fn()
                    run["status"] = "done"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                run["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                run["status"] = "failed"
            finally:
                run["finished"] = time.time()

        threading.Thread(target=_work, daemon=True,
                         name=f"neuralosd-run-{run_id}").start()
        return run_id

    def get(self, run_id):
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run else None

    def cancel(self, run_id):
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            run["cancel"] = True
            if run["status"] == "queued":
                run["status"] = "cancelled"
            return True
