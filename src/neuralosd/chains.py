"""Chain runner — declared probe DAGs with templated args.

A chain is a named sequence of probe calls where each step's args can
reference previous results via `{{step_name.field}}` templates. The model
routes each node; the DAG guarantees the path. All steps run through the
same deterministic execution the router uses — the model never improvises
the path.

usage:
    from neuralosd.chains import ChainRunner

    runner = ChainRunner(probes_by_name)
    result = runner.run("genre_deep_dive", {})

Chain definition (dict or @chain decorated):
    @chain(name="genre_deep_dive", steps=[
        {"probe": "top_genres"},
        {"probe": "tracks_by_genre", "args": {"genre": "{{top_genres.genre}}"}},
    ])
"""
import re
import time
import uuid
from typing import Any, Callable, Dict, List


class ChainError(Exception):
    def __init__(self, step: str, message: str):
        self.step = step
        super().__init__(f"chain step '{step}': {message}")


TEMPLATE_RE = re.compile(r"\{\{(\w+)\.([\w.]+)\}\}")


def resolve_template(value, context: Dict[str, Any]):
    """Replace {{step_name.path.to.field}} with values from previous results.
    Supports dot-notation deep access into nested result dicts."""
    if not isinstance(value, str):
        return value
    def sub(m):
        step, path = m.group(1), m.group(2)
        if step not in context:
            raise ChainError(step, f"referenced before execution")
        prev = context[step]
        for key in path.split("."):
            if isinstance(prev, dict) and key in prev:
                prev = prev[key]
            elif isinstance(prev, list) and key.isdigit():
                prev = prev[int(key)]
            else:
                raise ChainError(step, f"path '{path}' not found in step result")
        return str(prev)
    return TEMPLATE_RE.sub(sub, value)


class ChainRunner:
    def __init__(self, probes_by_name: Dict[str, Callable]):
        self.by_name = probes_by_name
        self.chains = {}      # name -> steps list

    def register(self, name: str, steps: List[Dict]):
        self.chains[name] = steps

    def run(self, name: str, initial_args: Dict = None) -> Dict:
        steps = self.chains.get(name)
        if not steps:
            raise ChainError(name, "chain not registered")
        context = {}
        results = []
        t0 = time.time()
        for i, step in enumerate(steps):
            probe_name = step["probe"]
            fn = self.by_name.get(probe_name)
            if not fn:
                raise ChainError(probe_name, "probe not found")
            args = {}
            for k, v in (step.get("args") or {}).items():
                args[k] = resolve_template(v, context)
            try:
                r = fn(**args)
            except Exception as exc:
                raise ChainError(probe_name, f"execution failed: {exc}") from exc
            ctx_key = f"step_{i}"
            context[ctx_key] = r
            results.append({"step": i, "probe": probe_name, "result": r})
        return {"chain": name, "steps": results,
                "latency_ms": int((time.time() - t0) * 1000)}


def chain(name: str, steps: List[Dict]) -> Callable:
    """Decorator to register a chain."""
    def deco(fn):
        fn._chain_steps = steps
        fn._chain_name = name
        return fn
    return deco
