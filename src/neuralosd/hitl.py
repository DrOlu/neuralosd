"""HITL confirm — the guardrail that asks a human before risky execution.

When a probe is flagged `confirm=True` or the confidence is below the
per-probe gate, the framework returns a **pending confirmation** (HTTP 202)
with a token. The caller (human or agent) confirms via
`POST /v1/confirm/{token}`, and the probe executes.
"""
import secrets
import time
from typing import Any, Callable, Dict


class ConfirmRequired(Exception):
    """Raised by the guardrail chain when a probe needs human approval."""
    def __init__(self, probe_name: str, args: Dict, reason: str):
        self.probe_name = probe_name
        self.args = args
        self.reason = reason
        self.token = secrets.token_hex(8)
        super().__init__(f"confirmation required for {probe_name} ({reason})")


class ConfirmStore:
    """Holds pending confirmations with expiry."""

    def __init__(self, ttl: int = 300):
        self.ttl = ttl
        self._pending = {}    # token -> {"probe_fn", "args", "ts", "reason"}

    def create(self, probe_fn: Callable, args: Dict, reason: str) -> Dict:
        token = secrets.token_hex(8)
        self._pending[token] = {
            "probe_fn": probe_fn, "args": args,
            "ts": time.time(), "reason": reason,
        }
        return {"confirm_token": token, "probe": probe_fn.__name__,
                "reason": reason, "ttl": self.ttl}

    def confirm(self, token: str) -> Dict:
        entry = self._pending.pop(token, None)
        if not entry or time.time() - entry["ts"] > self.ttl:
            return {"error": "invalid or expired confirm token"}
        return entry["probe_fn"](**entry["args"])

    def pending_count(self):
        return len(self._pending)
