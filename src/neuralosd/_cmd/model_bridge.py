"""Bridge from the @probe menu to the on-device neuralOS (needle) model.

The model is a **fallback router**, never the primary path. Lexical retrieval
and deterministic argument extraction run first; only when they cannot produce
an answer is the model asked. It then chooses among the instance's *real*
probes and fills their arguments. The probe itself still executes in the data
layer, so the answer stays verified — the model routes, it does not invent.

Notes learned from the live engine (needle 3.10):

* ``function_calls`` is **empty even on success** — the answer is in
  ``results``, so gate on that and never on ``function_calls``.
* ``results`` are JSON *strings*, one per tool call. They must be parsed.
* Because ``function_calls`` is empty we cannot read which probe ran from the
  envelope, so the wrapper records the calls itself.
* Loading a ``Needle`` engine is expensive (model + tool index), so engines are
  cached per tool-set and reused across questions.
* ``ThreadingHTTPServer`` can serve concurrent asks, and the engine is not
  thread-safe, so runs are serialised with a lock.
"""
import inspect
import json
import threading
from typing import Any, Dict, List, Optional

__all__ = ["model_ask", "clear_cache"]

# cache key -> {"engine": Needle, "called": [names]}
_CACHE: Dict[Any, Dict[str, Any]] = {}

# The needle engine is NOT thread-safe — not merely its inference: building
# two engines on two threads segfaults the process (reproduced live: six
# parallel /ask calls against `serve --model` killed the server, while the
# deterministic path stayed healthy). Serialising only run() was not enough,
# so ALL model work — construction included — sits behind one lock. The model
# is a fallback, so serialising it costs little and the deterministic path
# remains fully concurrent.
_MODEL_LOCK = threading.RLock()


def clear_cache():
    """Drop cached engines (closing them). Used by tests and after menu edits."""
    with _MODEL_LOCK:
        items = list(_CACHE.values())
        _CACHE.clear()
        for item in items:
            try:
                item["engine"].close()
            except Exception:  # noqa: BLE001
                pass


def _as_json(value) -> str:
    return json.dumps(value, default=str)


def _parse(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}
    return raw


def _key(metas) -> tuple:
    """Identify a tool-set so an engine can be reused across questions."""
    return tuple((m.name,
                  tuple(m.triggers or ()),
                  tuple(sorted((m.args or {}).keys())))
                 for m in metas)


def _wrap_probe(meta, called: List[str]):
    """Expose one probe as a callable tool, recording that the model used it."""
    fn = getattr(meta, "function", None)
    if fn is None:
        return None

    def call(**kwargs):
        called.append(meta.name)
        try:
            out = fn(**kwargs)
        except Exception as e:  # noqa: BLE001 — surfaced to the model as data
            out = {"error": f"{type(e).__name__}: {e}"}
        return _as_json(out)

    call.__name__ = meta.name
    call.__doc__ = meta.description or meta.name
    try:
        call.__signature__ = inspect.signature(fn)
    except (TypeError, ValueError):
        pass
    call._neuralosd_probe = meta.name
    return call


def _get_engine(metas):
    """Build (once per tool-set) or reuse a needle engine.

    Caller must hold ``_MODEL_LOCK``.
    """
    import needle

    key = _key(metas)
    hit = _CACHE.get(key)
    if hit:
        return hit

    called: List[str] = []
    tools = []
    for m in metas:
        c = _wrap_probe(m, called)
        if c is None:
            continue
        tools.append(needle.tool(
            c, triggers=list(getattr(m, "triggers", []) or [])))
    if not tools:
        return None

    entry = {"engine": needle.Needle(tools=tools), "called": called}
    _CACHE[key] = entry
    return entry


def model_ask(question: str, metas, max_steps: int = 8,
              max_new_tokens: int = 512) -> Optional[List[Dict[str, Any]]]:
    """Ask the on-device model to route ``question`` over ``metas``.

    Returns a list of result dicts (each tagged with ``_tool``) or ``None``
    when the model produced no tool result. Raises ``RuntimeError`` only when
    the engine itself reports an error.
    """
    metas = [m for m in (metas or []) if getattr(m, "function", None) is not None]
    if not metas:
        return None

    # One lock for the whole interaction: build, run and read-back.
    with _MODEL_LOCK:
        entry = _get_engine(metas)
        if entry is None:
            return None
        engine, called = entry["engine"], entry["called"]
        called.clear()
        out = engine.run(question, max_steps=max_steps,
                         max_new_tokens=max_new_tokens)
        calls = list(called)

    if not isinstance(out, dict):
        return None
    if out.get("error"):
        raise RuntimeError(f"on-device model error: {out['error']}")
    if not out.get("success"):
        return None

    results: List[Dict[str, Any]] = []
    for i, raw in enumerate(out.get("results") or []):
        parsed = _parse(raw)
        if not isinstance(parsed, dict):
            parsed = {"result": parsed}
        # `function_calls` is empty, so attribute by call order when we can.
        tool = calls[i] if i < len(calls) else (calls[-1] if calls else None)
        if tool and "_tool" not in parsed:
            parsed = {**parsed, "_tool": tool}
        results.append(parsed)
    return results or None
