"""neuralosd_sdk — the thin client for a neuralOS instance server.

Zero dependencies (urllib only), Python 3.9+. One class, one method that
matters:

    from neuralosd_sdk import NeuralOSDClient

    client = NeuralOSDClient("http://127.0.0.1:8877", token="secret")
    answer = client.ask("total revenue by region")
    print(answer.probe, answer.results)

Error model (mirrors the server's honesty contract):
    AuthError          — 401: token missing/wrong
    RefusalError       — 422: the instance REFUSED (cannot answer confidently)
    TransportError     — connection failures after retries
    raises URLError-equivalents only as TransportError; 5xx retried with
    exponential backoff, 4xx never retried.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = ["NeuralOSDClient", "AskResult", "AuthError", "RefusalError",
           "TransportError"]


class AuthError(RuntimeError):
    """401 — the server requires a token and the one given was missing/wrong."""


class RefusalError(RuntimeError):
    """422 — the instance refused: it cannot answer confidently, and guessing
    is worse. `.envelope` carries the refusal reason and candidate scores."""

    def __init__(self, message: str, envelope: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.envelope = envelope or {}


class TransportError(RuntimeError):
    """The server could not be reached (after retries) or kept failing."""


@dataclass
class AskResult:
    """One verified answer. `results` is the probe's own data; `raw` is the
    full envelope (ask_id, mode, latency, …) for callers that need it."""
    question: str
    probe: Optional[str]
    results: Any = None
    mode: Optional[str] = None
    cached: bool = False
    latency_ms: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class NeuralOSDClient:
    """Thin, dependency-free client for `neuralosd serve`.

    Args:
        base_url:   e.g. "http://127.0.0.1:8877" (trailing slash ok)
        token:      bearer token if the server runs with --auth-token /
                    $NEURALOSD_API_TOKEN
        timeout:    per-request timeout, seconds
        max_retries: retries on connection errors and 5xx (exponential
                    backoff, 0.5s doubling); 4xx is NEVER retried
    """

    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: float = 30.0, max_retries: int = 2,
                 backoff: float = 0.5):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff = backoff

    # ── public API ──────────────────────────────────────────────────────
    def ask(self, question: str) -> AskResult:
        """Ask one question; returns a typed result or raises.

        RefusalError is SUCCESS-adjacent: the instance understood the
        question and honestly declined. Catch it to say 'I can't answer
        that' in your product instead of showing nothing.
        """
        env = self._request("POST", "/ask",
                            {"question": question},
                            retryable=True)
        return AskResult(
            question=question,
            probe=env.get("probe"),
            results=env.get("results"),
            mode=env.get("mode"),
            cached=bool(env.get("cached")),
            latency_ms=env.get("latency_ms"),
            raw=env,
        )

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/healthz")

    def ready(self) -> Dict[str, Any]:
        return self._request("GET", "/ready")

    def openapi(self) -> Dict[str, Any]:
        return self._request("GET", "/openapi.json")

    # ── transport ───────────────────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, payload: Any = None,
                 retryable: bool = False) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        attempt, last_err = 0, None
        while attempt <= (self.max_retries if retryable else 0):
            req = urllib.request.Request(url, data=body,
                                         headers=self._headers(),
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                try:
                    parsed = json.loads(detail)
                except (ValueError, TypeError):
                    parsed = {"error": detail}
                if e.code == 401:
                    raise AuthError(parsed.get(
                        "error", "unauthorized: token missing or wrong"))
                if e.code == 422:
                    raise RefusalError(
                        parsed.get("error", "instance refused"), parsed)
                if 500 <= e.code < 600 and retryable:
                    last_err = TransportError(
                        f"HTTP {e.code}: {parsed.get('error', detail)}")
                else:
                    raise TransportError(
                        f"HTTP {e.code}: {parsed.get('error', detail)}")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if not retryable:
                    raise TransportError(f"connection failed: {e}")
                last_err = TransportError(f"connection failed: {e}")
            attempt += 1
            if attempt <= self.max_retries:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
        raise last_err or TransportError("request failed")
