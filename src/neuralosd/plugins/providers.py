"""Model providers — closed-list routing, never free-form tool calls.

A provider may only name a probe that is already in the menu and supply
arguments that the probe's own spec accepts. The core then executes the probe,
applies the results gate and audits the answer. A provider cannot add a
capability, call a tool that is not in the menu, or bypass a refusal.
"""
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..router import extract_args


class ProviderError(RuntimeError):
    """The provider is unreachable or returned unusable output."""


class ModelProvider:
    name = "base"

    def available(self) -> bool:
        return True

    def select(self, question: str, menu: List[Dict[str, Any]],
               k: int = 8) -> Optional[Dict[str, Any]]:
        """Return {"probe": name, "args": {...}} or None to abstain."""
        raise NotImplementedError

    def complete(self, messages: List[Dict[str, str]], **opts) -> Optional[str]:
        return None


def _menu_text(menu: List[Dict[str, Any]]) -> str:
    lines = []
    for m in menu[:40]:
        args = m.get("args") or {}
        arg_text = ", ".join(
            f"{name}:{spec.get('type', 'string')}"
            + (f" one of {spec.get('values')}" if spec.get("values") else "")
            for name, spec in args.items())
        lines.append(f"- {m['name']}: {m.get('description', '')}"
                     + (f"  args({arg_text})" if arg_text else ""))
    return "\n".join(lines)


ROUTING_PROMPT = """You route a question to exactly ONE probe from the menu.
Reply with JSON only: {{"probe": "<probe name>", "args": {{...}}}}
or {{"probe": null}} when no probe fits. Never invent a probe, never
answer the question yourself, and only use arguments the probe declares.

MENU:
{menu}

QUESTION: {question}
JSON:"""


def _probe_entries(metas) -> List[Dict[str, Any]]:
    out = []
    for m in metas or []:
        if getattr(m, "function", None) is None:
            continue
        out.append({"name": m.name, "description": m.description,
                    "args": m.args, "triggers": list(m.triggers or [])})
    return out


def _parse_pick(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    probe = data.get("probe")
    if probe is None:
        return None
    if not isinstance(probe, str):
        return None
    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    return {"probe": probe, "args": args}


class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(self, model: str = "llama3.1", url: Optional[str] = None,
                 timeout: float = 120.0):
        self.model = model
        self.url = (url or os.environ.get("OLLAMA_URL")
                    or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        return True

    def select(self, question, menu, k=8):
        prompt = ROUTING_PROMPT.format(menu=_menu_text(menu),
                                       question=question)
        payload = {"model": self.model, "stream": False, "format": "json",
                   "messages": [{"role": "user", "content": prompt}],
                   "options": {"temperature": 0, "seed": 42}}
        req = urllib.request.Request(
            self.url + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Ollama unreachable at {self.url}: {exc}")
        except ValueError as exc:
            raise ProviderError(f"Ollama returned non-JSON: {exc}")
        content = ((data.get("message") or {}).get("content") or "")
        return _parse_pick(content)

    def complete(self, messages, **opts):
        payload = {"model": self.model, "stream": False, "messages": messages}
        payload.update(opts)
        req = urllib.request.Request(
            self.url + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Ollama unreachable at {self.url}: {exc}")
        return (data.get("message") or {}).get("content")


class OpenAICompatProvider(ModelProvider):
    """Any /v1/chat/completions server: vLLM, LM Studio, llama.cpp server,
    Ollama's OpenAI-compatible endpoint, a hosted gateway."""

    name = "openai"

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8000/v1",
                 api_key: Optional[str] = None, timeout: float = 120.0):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.timeout = timeout

    def select(self, question, menu, k=8):
        prompt = ROUTING_PROMPT.format(menu=_menu_text(menu),
                                       question=question)
        payload = {"model": self.model, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"provider unreachable at {self.base_url}: "
                                f"{exc}")
        except ValueError as exc:
            raise ProviderError(f"provider returned non-JSON: {exc}")
        choices = data.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content")
                   if choices else "")
        return _parse_pick(content)

    def complete(self, messages, **opts):
        payload = {"model": self.model, "messages": messages}
        payload.update(opts)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"provider unreachable at {self.base_url}: "
                                f"{exc}")
        choices = data.get("choices") or []
        return ((choices[0].get("message") or {}).get("content")
                if choices else None)


class DeterministicProvider(ModelProvider):
    name = "deterministic"

    def select(self, question, menu, k=8):
        return None


def provider_from_spec(spec: str) -> ModelProvider:
    """Parse `ollama[:model]`, `openai:BASE:MODEL`, `deterministic`."""
    if not spec:
        raise ProviderError("empty model provider")
    parts = spec.split(":")
    name = parts[0].strip().lower()
    if name == "deterministic":
        return DeterministicProvider()
    if name == "ollama":
        return OllamaProvider(model=(parts[1] if len(parts) > 1
                                     else "llama3.1"))
    if name == "openai":
        if len(parts) < 3 or not parts[2]:
            raise ProviderError(
                "openai provider needs openai:BASE_URL:MODEL, e.g. "
                "openai:http://127.0.0.1:8000/v1:llama3.1")
        # BASE_URL may itself contain a colon (http://host:port); the MODEL is
        # the LAST segment, so rejoin the middle.
        base = ":".join(parts[1:-1])
        return OpenAICompatProvider(model=parts[-1], base_url=base)
    raise ProviderError(f"unknown model provider: {name}")


def _safe_args(meta, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Only declared keys, with values the probe's own spec accepts."""
    specs = meta.args or {}
    if any(k not in specs for k in args):
        return None
    out = {}
    for name, spec in specs.items():
        if name in args:
            value = args[name]
        elif spec.get("default") is not None:
            value = spec["default"]
        elif spec.get("required", True):
            return None
        else:
            continue
        kind = spec.get("type", "string")
        if kind == "enum":
            if value not in (spec.get("values") or []):
                return None
        elif kind == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None
            lo, hi = spec.get("min", 1), spec.get("max", 100)
            if value < lo or value > hi:
                return None
        elif kind == "pattern":
            pattern = spec.get("pattern")
            if not isinstance(value, str) or (
                    pattern and not re.search(pattern, value)):
                return None
        elif not isinstance(value, (str, int, float, bool)):
            return None
        out[name] = value
    return out


def fallback_from_provider(provider: ModelProvider):
    """Adapt a provider to Router's `model_fallback(question, metas)`.

    The provider picks a probe; the core executes it. Invalid names, invalid
    arguments and probe errors all mean ABSTAIN (None), which the router turns
    into an honest refusal rather than a guess.
    """
    def _fallback(question, metas):
        menu = _probe_entries(metas)
        pick = provider.select(question, menu)
        if not pick:
            return None
        meta = next((m for m in metas or [] if m.name == pick["probe"]), None)
        if meta is None or getattr(meta, "function", None) is None:
            return None
        args = _safe_args(meta, pick.get("args") or {})
        if args is None:
            args = extract_args(meta, question)
        if args is None:
            return None
        try:
            result = meta.function(**args)
        except Exception:
            return None
        if isinstance(result, dict):
            results = [{**result, "_tool": meta.name}]
        elif isinstance(result, list):
            results = result
        else:
            return None
        return results or None

    return _fallback
