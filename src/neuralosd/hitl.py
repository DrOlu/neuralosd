"""HITL confirm — the guardrail that asks a human before risky execution.

When a probe is flagged `confirm=True` or the confidence is below the
per-probe gate, the framework returns a **pending confirmation** (HTTP 202)
with a token. The caller (human or agent) confirms via
`POST /v1/confirm/{token}`, and the probe executes.

The token is an explicit state machine:

    pending --confirm--> confirmed
    pending --expire----> expired

`confirmed` and `expired` are terminal. A second confirm reports
`already_used`; an expired token reports `expired`; an unknown token reports
`invalid`. The three are distinguishable so an agent can tell "I already did
this" from "try again" from "this never existed".
"""
import secrets
import threading
import time
from typing import Any, Callable, Dict

CONFIRM_STATES = ("pending", "confirmed", "expired")


class ConfirmRequired(Exception):
    """Raised by the guardrail chain when a probe needs human approval."""
    def __init__(self, probe_name: str, args: Dict, reason: str,
                 token: str = None):
        self.probe_name = probe_name
        self.args = args
        self.reason = reason
        self.token = token or secrets.token_hex(8)
        super().__init__(f"confirmation required for {probe_name} ({reason})")


class ConfirmStore:
    """Holds pending confirmations with expiry, as an explicit FSM."""

    def __init__(self, ttl: int = 300):
        self.ttl = ttl
        self._pending = {}    # token -> {probe_fn, args, ts, reason, state}
        self._used = {}       # token -> {ts, state: "confirmed"|"expired"}
        self._lock = threading.Lock()

    def _prune(self, now):
        # Terminal tokens are remembered long enough to explain a retry,
        # not forever.
        horizon = now - max(self.ttl * 2, 3600)
        for token in [t for t, u in self._used.items() if u["ts"] < horizon]:
            del self._used[token]

    def create(self, probe_fn: Callable, args: Dict, reason: str,
               token: str = None, probe_name: str = None) -> Dict:
        token = token or secrets.token_hex(8)
        now = time.time()
        with self._lock:
            self._prune(now)
            self._pending[token] = {
                "probe_fn": probe_fn, "args": args, "ts": now,
                "reason": reason, "state": "pending",
            }
        return {"confirm_token": token,
                "probe": probe_name or probe_fn.__name__,
                "reason": reason, "ttl": self.ttl}

    def confirm(self, token: str) -> Dict:
        now = time.time()
        with self._lock:
            entry = self._pending.get(token)
            if entry is None:
                used = self._used.get(token)
                if used is None:
                    return {"error": "invalid confirm token",
                            "code": "invalid"}
                code = ("already_used" if used["state"] == "confirmed"
                        else "expired")
                return {"error": (f"confirm token already used"
                                  if code == "already_used"
                                  else "confirm token expired"),
                        "code": code}
            if now - entry["ts"] > self.ttl:
                entry["state"] = "expired"
                self._used[token] = {"ts": now, "state": "expired"}
                self._pending.pop(token, None)
                return {"error": "confirm token expired", "code": "expired"}
            entry["state"] = "confirmed"
            self._used[token] = {"ts": now, "state": "confirmed"}
            self._pending.pop(token, None)
            probe_fn, args = entry["probe_fn"], entry["args"]
        # Execute outside the lock: a slow probe must not block every other
        # confirmation (or a status check).
        return probe_fn(**args)

    def pending_count(self):
        with self._lock:
            return sum(1 for e in self._pending.values()
                       if e["state"] == "pending")

    def expire(self):
        """Move every overdue token to `expired`; returns how many moved."""
        now = time.time()
        moved = 0
        with self._lock:
            self._prune(now)
            for token, entry in list(self._pending.items()):
                if now - entry["ts"] > self.ttl:
                    entry["state"] = "expired"
                    self._used[token] = {"ts": now, "state": "expired"}
                    self._pending.pop(token, None)
                    moved += 1
        return moved
